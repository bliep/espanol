"""Turns Deepgram transcribed in a non-Latin script are ignored.

language=multi recognises ~55 languages; noise or mumbling sometimes comes back as
Arabic (or another script). Spanish and English always contain Latin letters.
"""

import sys
from pathlib import Path

import pytest
from livekit.agents import StopResponse, llm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learner_model import EMPTY_LESSON  # noqa: E402
from prompts import GREETING, Level  # noqa: E402
from transcript_filter import is_foreign_script  # noqa: E402
from tutor_agents import Tutor  # noqa: E402

FOREIGN = ["مرحبا كيف حالك", "Привет", "你好", "مرحبا 123"]
LEARNER = ["Ayer fui al mercado.", "I don't know", "Quiero... how do you say travel", "¿Qué?", "Sí"]


@pytest.mark.parametrize("text", FOREIGN)
def test_foreign_script_detected(text: str) -> None:
    assert is_foreign_script(text)


@pytest.mark.parametrize("text", LEARNER)
def test_learner_text_kept(text: str) -> None:
    assert not is_foreign_script(text)


def turn(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


async def test_tutor_ignores_foreign_script_turn() -> None:
    tutor = Tutor(Level.A2, EMPTY_LESSON, GREETING)
    with pytest.raises(StopResponse):
        await tutor.on_user_turn_completed(llm.ChatContext(), turn(FOREIGN[0]))


async def test_tutor_answers_learner_turn() -> None:
    tutor = Tutor(Level.A2, EMPTY_LESSON, GREETING)
    await tutor.on_user_turn_completed(llm.ChatContext(), turn(LEARNER[0]))  # no StopResponse
