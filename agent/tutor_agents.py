"""Conversation agents: free talk (Tutor) and scenario role-play (RolePlay).

Both use the tutor LLM with the learner's memory in the prompt, and greet on entry,
so switching modes mid-call starts the new mode with a spoken line.
"""

from livekit.agents import Agent

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

RESUME = "Retoma la conversación libre con una frase corta y una pregunta."


def memory(lesson: LessonContext) -> tuple[list[Word], list[RecentError]]:
    """The lesson as prompt inputs: (spanish, english) words, (category, wrong, right) errors."""
    words = [(w.spanish, w.english) for w in lesson.due_words]
    errors = [(e.category.value, e.original, e.corrected) for e in lesson.recent_errors]
    return words, errors


class Tutor(Agent):
    def __init__(self, level: Level, lesson: LessonContext, greeting: str) -> None:
        super().__init__(instructions=build_instructions(level, *memory(lesson)))
        self._greeting = greeting

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=self._greeting)


class RolePlay(Agent):
    def __init__(self, level: Level, lesson: LessonContext, scenario: Scenario) -> None:
        instructions = build_roleplay_instructions(level, scenario, *memory(lesson))
        super().__init__(instructions=instructions)

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=ROLEPLAY_GREETING)
