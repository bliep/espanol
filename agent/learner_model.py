"""Learner model: what to practise this session, and how each turn updates it.

  open()        ──► learner profile + session row
  lesson()      ──► due words + recent errors ──► prompt, Deepgram keyterms, web panel
  record_turn() ──► vocab gap              → FSRS review "Again" (new card if needed)
                    due word used, no error → FSRS review "Good"
                    corrections            → error log
  record_drill()──► drill attempt + per-word scores; FSRS review of the drilled word
  weakest_words()─► lowest average drill scores (web panel)
  close()       ──► session end + summary

Word matching is plain text: accents, case and a leading article are ignored, but
there is no lemmatizer ("viajar" does not match "viajo").
"""

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

import fsrs
from pydantic import BaseModel

from feedback import ErrorCategory, Feedback, UserLanguage, VocabItem
from learner_store import (
    DrillAttempt,
    ErrorRecord,
    LearnerStore,
    WordRecord,
    WordScoreRecord,
)
from prompts import Level

log = logging.getLogger("tutor.learner")

DUE_WORDS_LIMIT = 15  # plan: 10–20 target words per session
RECENT_ERRORS_WINDOW = timedelta(days=14)
RECENT_ERROR_CATEGORIES = 3
WEAK_WORDS_WINDOW = timedelta(days=30)
WEAK_WORDS_LIMIT = 8

Clock = Callable[[], datetime]

ARTICLES = frozenset({"el", "la", "los", "las", "un", "una", "unos", "unas"})


class RecentError(BaseModel):
    category: ErrorCategory
    count: int
    original: str
    corrected: str


class LessonContext(BaseModel):
    """Session opener; wire format of the lesson_context RPC (web/lib/tutor-feedback.ts)."""

    due_words: list[VocabItem]
    recent_errors: list[RecentError]

    @property
    def is_empty(self) -> bool:
        return not self.due_words and not self.recent_errors

    def keyterms(self) -> list[str]:
        """Due words for Deepgram keyterm prompting, without articles."""
        return [_strip_article(w.spanish) for w in self.due_words]


EMPTY_LESSON = LessonContext(due_words=[], recent_errors=[])


class WeakWord(BaseModel):
    """Wire format of the word_scores RPC items (web/lib/tutor-drill.ts)."""

    word: str
    average: float  # 0–100
    attempts: int


def term_of(spanish: str) -> str:
    """Match key: "El Paraguas" → "paraguas", "¿Está?" → "esta"."""
    return _normalize(_strip_article(spanish))


def used_terms(text: str, terms: set[str]) -> set[str]:
    """Terms that occur as whole words in `text`: "Hoy llevo paraguas" → {"paraguas"}."""
    padded = f" {_normalize(text)} "
    return {t for t in terms if f" {t} " in padded}


class LearnerModel:
    def __init__(
        self, store: LearnerStore, learner_id: UUID, session_id: UUID, clock: Clock
    ) -> None:
        self._store = store
        self._clock = clock  # injectable for tests
        self._learner_id = learner_id
        self._session_id = session_id
        self._scheduler = fsrs.Scheduler()
        self._due: dict[str, VocabItem] = {}  # term → word, practised this session

    @classmethod
    async def open(
        cls,
        store: LearnerStore,
        learner_id: UUID,
        level: Level,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> "LearnerModel":
        await store.ensure_learner(learner_id, level)
        session_id = await store.start_session(learner_id, level)
        log.info("learner %s session %s", learner_id, session_id)
        return cls(store, learner_id, session_id, clock)

    async def lesson(self) -> LessonContext:
        now = self._clock()
        words = await self._store.due_words(self._learner_id, now, DUE_WORDS_LIMIT)
        errors = await self._store.recent_errors(
            self._learner_id, now - RECENT_ERRORS_WINDOW, RECENT_ERROR_CATEGORIES
        )

        due = [VocabItem(spanish=w.spanish, english=w.english) for w in words]
        self._due = {w.term: item for w, item in zip(words, due, strict=True)}
        recent = [RecentError(**asdict(e)) for e in errors]
        return LessonContext(due_words=due, recent_errors=recent)

    async def record_turn(self, learner: str, fb: Feedback) -> None:
        now = self._clock()

        # Gaps first: a word the learner lacked was not "used correctly" this turn.
        for item in fb.new_vocab:
            term = term_of(item.spanish)
            if not term:
                continue
            await self._review(term, item, fsrs.Rating.Again, now)
            self._due[term] = item  # practise it again later this session

        errors = [ErrorRecord(c.category, c.original, c.corrected, c.rule) for c in fb.corrections]
        await self._store.add_errors(self._learner_id, self._session_id, errors)

        if fb.user_language == UserLanguage.ENGLISH:
            return

        # Due words used in Spanish, outside any corrected fragment, count as recalled.
        wrong = " ".join(c.original for c in fb.corrections)
        for term in used_terms(learner, set(self._due)) - used_terms(wrong, set(self._due)):
            await self._review(term, self._due.pop(term), fsrs.Rating.Good, now)

    async def record_drill(
        self,
        attempt: DrillAttempt,
        words: list[tuple[str, float]],
        review: tuple[VocabItem, fsrs.Rating] | None = None,
    ) -> None:
        """words: (display word, score 0–100); review: the drilled word and its FSRS rating."""
        scores = [WordScoreRecord(term_of(w), w, score) for w, score in words if term_of(w)]
        await self._store.add_drill_attempt(self._learner_id, self._session_id, attempt, scores)

        if review:
            item, rating = review
            await self._review(term_of(item.spanish), item, rating, self._clock())

    async def weakest_words(self) -> list[WeakWord]:
        since = self._clock() - WEAK_WORDS_WINDOW
        stats = await self._store.weakest_words(self._learner_id, since, WEAK_WORDS_LIMIT)
        return [WeakWord(**asdict(s)) for s in stats]

    async def save_summary(self, summary_json: str) -> None:
        await self._store.save_summary(self._session_id, summary_json)

    async def close(self, summary_json: str | None = None) -> None:
        await self._store.end_session(self._session_id, summary_json)

    async def _review(self, term: str, item: VocabItem, rating: fsrs.Rating, now: datetime) -> None:
        saved = await self._store.get_word(self._learner_id, term)
        card = fsrs.Card.from_dict(saved.card) if saved else fsrs.Card()
        card, _ = self._scheduler.review_card(card, rating, now)

        word = WordRecord(term, item.spanish, item.english, card.to_dict(), card.due)
        await self._store.save_word(self._learner_id, word)
        log.info("review %s %s → due %s", term, rating.name, card.due.isoformat(timespec="minutes"))


def _strip_article(spanish: str) -> str:
    words = spanish.strip().split()
    if len(words) > 1 and words[0].lower() in ARTICLES:
        words = words[1:]
    return " ".join(words)


def _normalize(text: str) -> str:
    """Lowercase, no accents (STT and typing drop them; ñ becomes n), letters only."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    plain = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(re.findall(r"[a-z]+", plain))
