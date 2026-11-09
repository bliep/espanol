"""Lesson modes the learner switches between mid-call (set_mode RPC).

conversation  free talk (Phase 1 tutor)
roleplay      tutor plays a scenario role
translate_en  English sentence → learner says it in Spanish (drill)
translate_es  Spanish sentence → learner says what it means in English (drill)
repeat        learner repeats a Spanish sentence; Azure scores pronunciation (drill)
"""

from enum import StrEnum

from pydantic import BaseModel

from prompts import Scenario


class LessonMode(StrEnum):
    CONVERSATION = "conversation"
    ROLEPLAY = "roleplay"
    TRANSLATE_EN = "translate_en"
    TRANSLATE_ES = "translate_es"
    REPEAT = "repeat"


DRILL_MODES = frozenset({LessonMode.TRANSLATE_EN, LessonMode.TRANSLATE_ES, LessonMode.REPEAT})


class ModeRequest(BaseModel):
    """set_mode RPC payload, e.g. {"mode": "roleplay", "scenario": "doctor"}."""

    mode: LessonMode
    scenario: Scenario = Scenario.RESTAURANT  # roleplay only
