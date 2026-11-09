"""Conversation agents: free talk (Tutor) and scenario role-play (RolePlay).

Both use the tutor LLM with the learner's memory in the prompt, and greet on entry,
so switching modes mid-call starts the new mode with a spoken line. With a TurnRecorder
(RECORD_TURNS=1) their STT input is also saved for the eval set.
"""

from collections.abc import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings

from learner_model import LessonContext
from prompts import (
    ROLEPLAY_GREETING,
    Level,
    RecentError,
    Scenario,
    Word,
    build_instructions,
    build_roleplay_instructions,
)
from turn_recorder import TurnRecorder

RESUME = "Retoma la conversación libre con una frase corta y una pregunta."


def memory(lesson: LessonContext) -> tuple[list[Word], list[RecentError]]:
    """The lesson as prompt inputs: (spanish, english) words, (category, wrong, right) errors."""
    words = [(w.spanish, w.english) for w in lesson.due_words]
    errors = [(e.category.value, e.original, e.corrected) for e in lesson.recent_errors]
    return words, errors


class ConversationAgent(Agent):
    """Tutor-LLM agent whose learner audio can be recorded."""

    def __init__(self, instructions: str, recorder: TurnRecorder | None) -> None:
        super().__init__(instructions=instructions)
        self._recorder = recorder

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable:
        source = self._recorder.tee(audio) if self._recorder else audio
        async for event in Agent.default.stt_node(self, source, model_settings):
            yield event


class Tutor(ConversationAgent):
    def __init__(
        self,
        level: Level,
        lesson: LessonContext,
        greeting: str,
        recorder: TurnRecorder | None = None,
    ) -> None:
        super().__init__(build_instructions(level, *memory(lesson)), recorder)
        self._greeting = greeting

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=self._greeting)


class RolePlay(ConversationAgent):
    def __init__(
        self,
        level: Level,
        lesson: LessonContext,
        scenario: Scenario,
        recorder: TurnRecorder | None = None,
    ) -> None:
        instructions = build_roleplay_instructions(level, scenario, *memory(lesson))
        super().__init__(instructions, recorder)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=ROLEPLAY_GREETING)
