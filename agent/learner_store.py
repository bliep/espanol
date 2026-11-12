"""Azure SQL access for the learner model. The only module that runs SQL.

Callers get domain records; FSRS card state stays an opaque dict here.
Schema: db/migrations/. Driver: mssql-python (sync), run in worker threads.

  LearnerModel ──► LearnerStore ──asyncio.to_thread──► mssql-python pool ──TLS──► Azure SQL
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar
from uuid import UUID

import mssql_python

from feedback import ErrorCategory
from prompts import Level

POOL_MAX_SIZE = 4  # one session: feedback writes + RPCs, rarely concurrent

# The free-offer database pauses when idle; logins fail until it's back (seconds up to
# about a minute), with 40613 "database not currently available" or a TCP timeout (258).
RESUMING_ERRORS = ("(40613)", "Timeout error [258]")
RESUME_TIMEOUT_S = 90.0
RESUME_RETRY_S = 5.0

T = TypeVar("T")
log = logging.getLogger("tutor.store")


def is_resuming(error: str) -> bool:
    """True if a connection error means a paused database is waking up (worth retrying)."""
    return any(code in error for code in RESUMING_ERRORS)


class DbLogin(Enum):
    """Database logins, by the .env keys holding user and password."""

    AGENT = ("AZURE_SQL_USER", "AZURE_SQL_PASSWORD")  # read/write rows
    ADMIN = ("AZURE_SQL_ADMIN_USER", "AZURE_SQL_ADMIN_PASSWORD")  # migrations only


SERVER_KEY = "AZURE_SQL_SERVER"
DATABASE_KEY = "AZURE_SQL_DATABASE"


def connection_string(env: Mapping[str, str], login: DbLogin) -> str | None:
    """Driver connection string from .env-style settings; None if any is missing."""
    user_key, password_key = login.value
    values = [env.get(k) for k in (SERVER_KEY, DATABASE_KEY, user_key, password_key)]
    if not all(values):
        return None

    server, database, user, password = values
    return (
        f"Server=tcp:{server},1433;Database={database};Uid={user};Pwd={password};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )


@dataclass(frozen=True)
class WordRecord:
    term: str  # match key, e.g. "paraguas"
    spanish: str  # display, e.g. "el paraguas"
    english: str
    card: dict[str, Any]  # fsrs.Card.to_dict()
    due: datetime


@dataclass(frozen=True)
class ErrorRecord:
    category: ErrorCategory
    original: str
    corrected: str
    rule: str


@dataclass(frozen=True)
class DrillAttempt:
    mode: str  # modes.LessonMode value
    prompt: str
    expected: str
    answer: str
    score: float
    details: dict[str, Any]


@dataclass(frozen=True)
class SessionRecord:
    ended_at: datetime | None
    summary: dict[str, Any] | None  # SessionSummary


@dataclass(frozen=True)
class WordScoreRecord:
    term: str
    word: str
    score: float


@dataclass(frozen=True)
class WordStat:
    """A word's drill scores over a period."""

    word: str
    average: float
    attempts: int


@dataclass(frozen=True)
class ErrorStat:
    """One error category over a period, with its latest example."""

    category: ErrorCategory
    count: int
    original: str
    corrected: str


class LearnerStore:
    def __init__(self, conn_str: str) -> None:
        self._conn_str = conn_str

    @classmethod
    async def connect(cls, conn_str: str) -> "LearnerStore":
        """Store with a checked connection; waits while a paused database resumes."""
        mssql_python.pooling(max_size=POOL_MAX_SIZE)
        store = cls(conn_str)
        await asyncio.to_thread(store._wait_until_up)
        return store

    async def close(self) -> None:
        """Nothing to release: connections go back to the driver's pool after each call."""

    def _wait_until_up(self) -> None:
        deadline = time.monotonic() + RESUME_TIMEOUT_S
        while True:
            try:
                mssql_python.connect(self._conn_str).close()
                return
            except mssql_python.Error as e:
                if not is_resuming(str(e)) or time.monotonic() > deadline:
                    raise
            log.info("database resuming, retrying in %.0f s", RESUME_RETRY_S)
            time.sleep(RESUME_RETRY_S)

    async def _run(self, work: Callable[[Any], T]) -> T:
        """Runs `work(cursor)` in one transaction on a worker thread."""
        return await asyncio.to_thread(self._transaction, work)

    def _transaction(self, work: Callable[[Any], T]) -> T:
        conn = mssql_python.connect(self._conn_str)
        try:
            result = work(conn.cursor())
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def ensure_learner(self, learner_id: UUID, level: Level) -> None:
        """Create the profile on first visit; keep the latest chosen level."""
        await self._run(
            lambda cur: cur.execute(
                "merge learners with (holdlock) as t using (select ? as id, ? as level) as s"
                " on t.id = s.id"
                " when matched then update set level = s.level"
                " when not matched then insert (id, level) values (s.id, s.level);",
                (learner_id, level.value),
            )
        )

    async def delete_learner(self, learner_id: UUID) -> None:
        """Removes the learner and all their rows (cascades)."""
        await self._run(lambda cur: cur.execute("delete from learners where id = ?", (learner_id,)))

    async def start_session(self, learner_id: UUID, level: Level) -> UUID:
        def work(cur: Any) -> UUID:
            cur.execute(
                "insert into sessions (learner_id, level) output inserted.id values (?, ?)",
                (learner_id, level.value),
            )
            return cur.fetchone()[0]

        return await self._run(work)

    async def end_session(self, session_id: UUID, summary: str | None, cost: str | None) -> None:
        """summary: SessionSummary JSON, or None to keep any summary saved earlier.
        cost: CostBreakdown JSON."""
        await self._run(
            lambda cur: cur.execute(
                "update sessions set ended_at = sysdatetimeoffset(),"
                " summary = coalesce(?, summary), cost = coalesce(?, cost) where id = ?",
                (summary, cost, session_id),
            )
        )

    async def session_costs(self, since: datetime) -> list[dict[str, Any]]:
        """CostBreakdown dicts of sessions started since `since`, all learners."""

        def work(cur: Any) -> list[dict[str, Any]]:
            cur.execute(
                "select cost from sessions where cost is not null and started_at >= ?", (since,)
            )
            return [json.loads(cost) for (cost,) in cur.fetchall()]

        return await self._run(work)

    async def sessions_of(self, learner_id: UUID) -> list[SessionRecord]:
        """The learner's sessions, oldest first."""

        def work(cur: Any) -> list[SessionRecord]:
            cur.execute(
                "select ended_at, summary from sessions where learner_id = ? order by started_at",
                (learner_id,),
            )
            return [
                SessionRecord(ended_at, json.loads(summary) if summary else None)
                for ended_at, summary in cur.fetchall()
            ]

        return await self._run(work)

    async def save_summary(self, session_id: UUID, summary: str) -> None:
        await self._run(
            lambda cur: cur.execute(
                "update sessions set summary = ? where id = ?", (summary, session_id)
            )
        )

    async def due_words(self, learner_id: UUID, now: datetime, limit: int) -> list[WordRecord]:
        """Words due by `now`, most overdue first."""

        def work(cur: Any) -> list[WordRecord]:
            cur.execute(
                "select top (?) term, spanish, english, card, due from vocab"
                " where learner_id = ? and due <= ? order by due",
                (limit, learner_id, now),
            )
            return [_word(r) for r in cur.fetchall()]

        return await self._run(work)

    async def get_word(self, learner_id: UUID, term: str) -> WordRecord | None:
        def work(cur: Any) -> WordRecord | None:
            cur.execute(
                "select term, spanish, english, card, due from vocab"
                " where learner_id = ? and term = ?",
                (learner_id, term),
            )
            row = cur.fetchone()
            return _word(row) if row else None

        return await self._run(work)

    async def save_word(self, learner_id: UUID, word: WordRecord) -> None:
        await self._run(
            lambda cur: cur.execute(
                "merge vocab with (holdlock) as t"
                " using (select ? as learner_id, ? as term, ? as spanish, ? as english,"
                " ? as card, ? as due) as s"
                " on t.learner_id = s.learner_id and t.term = s.term"
                " when matched then update set spanish = s.spanish, english = s.english,"
                " card = s.card, due = s.due"
                " when not matched then insert (learner_id, term, spanish, english, card, due)"
                " values (s.learner_id, s.term, s.spanish, s.english, s.card, s.due);",
                (
                    learner_id,
                    word.term,
                    word.spanish,
                    word.english,
                    json.dumps(word.card),
                    word.due,
                ),
            )  # fmt: skip
        )

    async def add_errors(
        self, learner_id: UUID, session_id: UUID, errors: list[ErrorRecord]
    ) -> None:
        if not errors:
            return
        await self._run(
            lambda cur: cur.executemany(
                "insert into errors (learner_id, session_id, category, original, corrected, [rule])"
                " values (?, ?, ?, ?, ?, ?)",
                [
                    (learner_id, session_id, e.category.value, e.original, e.corrected, e.rule)
                    for e in errors
                ],
            )  # fmt: skip
        )

    async def recent_errors(self, learner_id: UUID, since: datetime, limit: int) -> list[ErrorStat]:
        """Most frequent error categories since `since`, each with its latest example."""

        # Per category: count, latest time and latest example (row 1 by time, newest first).
        def work(cur: Any) -> list[ErrorStat]:
            cur.execute(
                "with ranked as (select category, original, corrected,"
                " count(*) over (partition by category) as n,"
                " max(created_at) over (partition by category) as latest,"
                " row_number() over (partition by category order by created_at desc) as pos"
                " from errors where learner_id = ? and created_at >= ?)"
                " select top (?) category, n, original, corrected from ranked"
                " where pos = 1 order by n desc, latest desc",
                (learner_id, since, limit),
            )
            return [
                ErrorStat(ErrorCategory(category), n, original, corrected)
                for category, n, original, corrected in cur.fetchall()
            ]

        return await self._run(work)

    async def add_drill_attempt(
        self,
        learner_id: UUID,
        session_id: UUID,
        attempt: DrillAttempt,
        words: list[WordScoreRecord],
    ) -> None:
        """One answered item and its per-word scores, atomically."""

        def work(cur: Any) -> None:
            cur.execute(
                "insert into drill_attempts"
                " (learner_id, session_id, mode, prompt, expected, answer, score, details)"
                " output inserted.id values (?, ?, ?, ?, ?, ?, ?, ?)",
                (learner_id, session_id, attempt.mode, attempt.prompt, attempt.expected,
                 attempt.answer, attempt.score, json.dumps(attempt.details)),
            )  # fmt: skip
            attempt_id = cur.fetchone()[0]
            if not words:
                return

            cur.executemany(
                "insert into word_scores (learner_id, attempt_id, term, word, mode, score)"
                " values (?, ?, ?, ?, ?, ?)",
                [(learner_id, attempt_id, w.term, w.word, attempt.mode, w.score) for w in words],
            )

        await self._run(work)

    async def weakest_words(self, learner_id: UUID, since: datetime, limit: int) -> list[WordStat]:
        """Words with the lowest average drill score since `since`, any mode."""

        # Per term: average, attempts and the latest display form.
        def work(cur: Any) -> list[WordStat]:
            cur.execute(
                "with ranked as (select word,"
                " avg(cast(score as float)) over (partition by term) as average,"
                " count(*) over (partition by term) as attempts,"
                " row_number() over (partition by term order by created_at desc) as pos"
                " from word_scores where learner_id = ? and created_at >= ?)"
                " select top (?) word, average, attempts from ranked"
                " where pos = 1 order by average, attempts desc",
                (learner_id, since, limit),
            )
            return [
                WordStat(word, round(average, 1), attempts)
                for word, average, attempts in cur.fetchall()
            ]

        return await self._run(work)


def _word(row: Any) -> WordRecord:
    term, spanish, english, card, due = row
    return WordRecord(term, spanish, english, json.loads(card), due)
