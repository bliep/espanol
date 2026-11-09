"""Postgres access for the learner model (Supabase). The only module that runs SQL.

Callers get domain records; FSRS card state stays an opaque dict here.
Schema: supabase/migrations/.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from feedback import ErrorCategory
from prompts import Level

POOL_MAX_SIZE = 4  # one session: feedback writes + RPCs, rarely concurrent


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
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "LearnerStore":
        # statement_cache_size=0: the Supabase pooler does not support prepared statements.
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=POOL_MAX_SIZE, statement_cache_size=0
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def ensure_learner(self, learner_id: UUID, level: Level) -> None:
        """Create the profile on first visit; keep the latest chosen level."""
        await self._pool.execute(
            "insert into learners (id, level) values ($1, $2)"
            " on conflict (id) do update set level = excluded.level",
            learner_id,
            level.value,
        )

    async def start_session(self, learner_id: UUID, level: Level) -> UUID:
        return await self._pool.fetchval(
            "insert into sessions (learner_id, level) values ($1, $2) returning id",
            learner_id,
            level.value,
        )

    async def end_session(self, session_id: UUID, summary: str | None) -> None:
        """summary: SessionSummary JSON, or None to keep any summary saved earlier."""
        await self._pool.execute(
            "update sessions set ended_at = now(), summary = coalesce($2::jsonb, summary)"
            " where id = $1",
            session_id,
            summary,
        )

    async def save_summary(self, session_id: UUID, summary: str) -> None:
        await self._pool.execute(
            "update sessions set summary = $2::jsonb where id = $1", session_id, summary
        )

    async def due_words(self, learner_id: UUID, now: datetime, limit: int) -> list[WordRecord]:
        """Words due by `now`, most overdue first."""
        rows = await self._pool.fetch(
            "select term, spanish, english, card, due from vocab"
            " where learner_id = $1 and due <= $2 order by due limit $3",
            learner_id,
            now,
            limit,
        )
        return [_word(r) for r in rows]

    async def get_word(self, learner_id: UUID, term: str) -> WordRecord | None:
        row = await self._pool.fetchrow(
            "select term, spanish, english, card, due from vocab"
            " where learner_id = $1 and term = $2",
            learner_id,
            term,
        )
        return _word(row) if row else None

    async def save_word(self, learner_id: UUID, word: WordRecord) -> None:
        await self._pool.execute(
            "insert into vocab (learner_id, term, spanish, english, card, due)"
            " values ($1, $2, $3, $4, $5::jsonb, $6)"
            " on conflict (learner_id, term) do update set"
            " spanish = excluded.spanish, english = excluded.english,"
            " card = excluded.card, due = excluded.due",
            learner_id,
            word.term,
            word.spanish,
            word.english,
            json.dumps(word.card),
            word.due,
        )

    async def add_errors(
        self, learner_id: UUID, session_id: UUID, errors: list[ErrorRecord]
    ) -> None:
        if not errors:
            return
        await self._pool.executemany(
            "insert into errors (learner_id, session_id, category, original, corrected, rule)"
            " values ($1, $2, $3, $4, $5, $6)",
            [(learner_id, session_id, e.category.value, e.original, e.corrected, e.rule)
             for e in errors],
        )  # fmt: skip

    async def recent_errors(self, learner_id: UUID, since: datetime, limit: int) -> list[ErrorStat]:
        """Most frequent error categories since `since`, each with its latest example."""
        rows = await self._pool.fetch(
            "select category, count(*) as n,"
            " (array_agg(original order by created_at desc))[1] as original,"
            " (array_agg(corrected order by created_at desc))[1] as corrected"
            " from errors where learner_id = $1 and created_at >= $2"
            " group by category order by n desc, max(created_at) desc limit $3",
            learner_id,
            since,
            limit,
        )
        return [
            ErrorStat(ErrorCategory(r["category"]), r["n"], r["original"], r["corrected"])
            for r in rows
        ]

    async def add_drill_attempt(
        self,
        learner_id: UUID,
        session_id: UUID,
        attempt: DrillAttempt,
        words: list[WordScoreRecord],
    ) -> None:
        """One answered item and its per-word scores, atomically."""
        async with self._pool.acquire() as conn, conn.transaction():
            attempt_id = await conn.fetchval(
                "insert into drill_attempts"
                " (learner_id, session_id, mode, prompt, expected, answer, score, details)"
                " values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) returning id",
                learner_id,
                session_id,
                attempt.mode,
                attempt.prompt,
                attempt.expected,
                attempt.answer,
                attempt.score,
                json.dumps(attempt.details),
            )
            await conn.executemany(
                "insert into word_scores (learner_id, attempt_id, term, word, mode, score)"
                " values ($1, $2, $3, $4, $5, $6)",
                [(learner_id, attempt_id, w.term, w.word, attempt.mode, w.score) for w in words],
            )

    async def weakest_words(self, learner_id: UUID, since: datetime, limit: int) -> list[WordStat]:
        """Words with the lowest average drill score since `since`, any mode."""
        rows = await self._pool.fetch(
            "select (array_agg(word order by created_at desc))[1] as word,"
            " avg(score) as average, count(*) as attempts"
            " from word_scores where learner_id = $1 and created_at >= $2"
            " group by term order by average, attempts desc limit $3",
            learner_id,
            since,
            limit,
        )
        return [WordStat(r["word"], round(r["average"], 1), r["attempts"]) for r in rows]


def _word(row: asyncpg.Record) -> WordRecord:
    return WordRecord(
        row["term"], row["spanish"], row["english"], json.loads(row["card"]), row["due"]
    )
