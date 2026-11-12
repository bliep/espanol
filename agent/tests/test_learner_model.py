"""Learner model tests.

Matching tests are pure. The flow test runs against the Azure SQL database in .env
(agent login) with a throwaway learner, deleted afterwards (rows cascade).
"""

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fsrs  # noqa: E402

from feedback import Correction, ErrorCategory, Feedback, UserLanguage, VocabItem  # noqa: E402
from learner_model import LearnerModel, term_of, used_terms  # noqa: E402
from learner_store import DbLogin, DrillAttempt, LearnerStore, connection_string  # noqa: E402
from prompts import Level  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
DB = connection_string(os.environ, DbLogin.AGENT)

UMBRELLA = VocabItem(spanish="el paraguas", english="umbrella")
NEXT_DAY = timedelta(days=1)  # FSRS "Again" on a new card: due in minutes


class Clock:
    """Fake time the test moves forward."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def test_term_of() -> None:
    assert term_of("El Paraguas") == "paraguas"
    assert term_of("¿Está?") == "esta"
    assert term_of("la") == "la"  # a lone article is the word itself
    assert term_of("ir de compras") == "ir de compras"


def test_used_terms() -> None:
    terms = {"paraguas", "ir de compras", "pan"}
    assert used_terms("Hoy llevo mi PARAGUAS.", terms) == {"paraguas"}
    assert used_terms("Voy a ir de compras", terms) == {"ir de compras"}
    assert used_terms("Me gusta el panadero", terms) == set()  # whole words only


def fb(
    language: UserLanguage = UserLanguage.SPANISH,
    vocab: list[VocabItem] | None = None,
    corrections: list[Correction] | None = None,
) -> Feedback:
    return Feedback(
        corrections=corrections or [],
        new_vocab=vocab or [],
        english_gloss="",
        user_language=language,
    )


@pytest.fixture
async def db():
    if not DB:
        pytest.skip("Azure SQL agent login not set")

    store = await LearnerStore.connect(DB)
    learner_id = uuid.uuid4()

    yield store, learner_id

    await store.delete_learner(learner_id)
    await store.close()


async def test_session_flow(db) -> None:
    store, learner_id = db
    clock = Clock()

    # Session 1: a vocab gap and a ser/estar error.
    first = await LearnerModel.open(store, learner_id, Level.A2, clock)
    assert (await first.lesson()).is_empty

    gap = fb(UserLanguage.MIXED, vocab=[UMBRELLA])
    await first.record_turn("Hoy llevo un... umbrella", gap)
    error = Correction(
        original="es cansado",
        corrected="está cansado",
        rule="Temporary state: estar.",
        category=ErrorCategory.SER_ESTAR,
    )
    await first.record_turn("Yo es cansado", fb(corrections=[error]))
    await first.close('{"patterns": []}')

    # Session 2, next day: opens with the due word and the recent error.
    clock.now += NEXT_DAY
    second = await LearnerModel.open(store, learner_id, Level.A2, clock)

    lesson = await second.lesson()
    assert lesson.due_words == [UMBRELLA]
    assert lesson.keyterms() == ["paraguas"]
    assert [(e.category, e.count) for e in lesson.recent_errors] == [(ErrorCategory.SER_ESTAR, 1)]

    # Used correctly in Spanish → reviewed "Good", no longer due soon.
    await second.record_turn("Sí, hoy llevo paraguas.", fb())
    word = await store.get_word(learner_id, "paraguas")
    assert word is not None and word.due > clock.now
    await second.close()

    # Same day again: nothing due.
    third = await LearnerModel.open(store, learner_id, Level.A2, clock)
    assert (await third.lesson()).due_words == []


async def test_word_inside_correction_is_not_recalled(db) -> None:
    store, learner_id = db
    model = await LearnerModel.open(store, learner_id, Level.A2)
    await model.record_turn("umbrella", fb(UserLanguage.ENGLISH, vocab=[UMBRELLA]))
    due_before = (await store.get_word(learner_id, "paraguas")).due

    wrong = Correction(
        original="la paraguas",
        corrected="el paraguas",
        rule="Paraguas is masculine.",
        category=ErrorCategory.AGREEMENT,
    )
    await model.record_turn("Tengo la paraguas", fb(corrections=[wrong]))

    assert (await store.get_word(learner_id, "paraguas")).due == due_before


async def test_drill_scores_per_word(db) -> None:
    store, learner_id = db
    model = await LearnerModel.open(store, learner_id, Level.A2)

    repeat = DrillAttempt("repeat", "Hoy llevo paraguas.", "Hoy llevo paraguas.", "", 60, {})
    await model.record_drill(repeat, [("Hoy", 100), ("llevo", 90), ("paraguas", 20)])
    translate = DrillAttempt("translate_en", "I have an umbrella.", "Tengo un paraguas.", "", 0, {})
    await model.record_drill(translate, [("paraguas", 0)], review=(UMBRELLA, fsrs.Rating.Again))

    weakest = await model.weakest_words()
    assert [(w.word, w.average, w.attempts) for w in weakest][:2] == [
        ("paraguas", 10, 2),
        ("llevo", 90, 1),
    ]
    assert await store.get_word(learner_id, "paraguas") is not None  # FSRS card created
