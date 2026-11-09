"""End-of-session summary: 3–5 patterns for the learner to work on.

Input is the session's per-turn feedback (feedback_channel.py) plus the transcript.
Runs on demand (RPC from the web panel), so it uses Sonnet for quality; it is not
on the voice path.
"""

import anthropic
from pydantic import BaseModel, Field

from costs import CostMeter
from feedback import VocabItem
from feedback_channel import FeedbackMessage
from prompts import Level

SUMMARY_MODEL = "claude-sonnet-5-5"
SUMMARY_MAX_TOKENS = 4000
SUMMARY_EFFORT = "low"


class Pattern(BaseModel):
    title: str = Field(description="Short name, e.g. 'ser vs estar for states'")
    explanation: str = Field(description="One or two English sentences")
    examples: list[str] = Field(description="Learner's own wrong → right pairs from this session")


class _ModelSummary(BaseModel):
    """What the model writes; new words are collected in code instead."""

    patterns: list[Pattern] = Field(description="3–5 items, most frequent first")
    encouragement: str = Field(description="One sentence on what went well, in English")


class SessionSummary(_ModelSummary):
    """Wire format of the session_summary RPC (mirrored in web/lib/tutor-feedback.ts)."""

    new_words: list[VocabItem]  # the session's vocab gaps, deduplicated


EMPTY_SUMMARY = SessionSummary(
    patterns=[], new_words=[], encouragement="Say a few sentences first, then ask again."
)

_SYSTEM = """\
You write the end-of-session summary for an English-speaking learner of Spain Spanish
(CEFR {level}). From the transcript and the per-turn corrections, find 3 to 5
patterns worth practising: recurring error types, frequent vocabulary gaps, or
avoiding Spanish (answering in English). Use the learner's own sentences as examples.
If there are fewer real patterns, return fewer. Be specific and brief.
"""

Transcript = list[tuple[str, str]]  # (role, text)


class SessionSummarizer:
    def __init__(
        self, client: anthropic.AsyncAnthropic | None = None, meter: CostMeter | None = None
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()
        self._meter = meter

    async def summarize(
        self, feedback: list[FeedbackMessage], transcript: Transcript, level: Level
    ) -> SessionSummary:
        if not feedback:
            return EMPTY_SUMMARY

        lines = "\n".join(f"{role}: {text}" for role, text in transcript)
        notes = "\n".join(m.model_dump_json(exclude={"turn_id"}) for m in feedback)
        response = await self._client.messages.parse(
            model=SUMMARY_MODEL,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=_SYSTEM.format(level=level),
            messages=[
                {
                    "role": "user",
                    "content": f"Transcript:\n{lines}\n\nPer-turn feedback:\n{notes}",
                }
            ],
            output_format=_ModelSummary,
            output_config={"effort": SUMMARY_EFFORT},
        )
        if self._meter:
            self._meter.add_claude(SUMMARY_MODEL, response.usage)
        return SessionSummary(**response.parsed_output.model_dump(), new_words=_gaps(feedback))


def _gaps(feedback: list[FeedbackMessage]) -> list[VocabItem]:
    """Vocab gaps in order of first appearance, one entry per Spanish word."""
    seen: dict[str, VocabItem] = {}
    for message in feedback:
        for item in message.feedback.new_vocab:
            seen.setdefault(item.spanish.lower(), item)
    return list(seen.values())
