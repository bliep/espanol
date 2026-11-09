"""End-of-session summary: built on request (web RPC) or at shutdown, saved to the profile.

web "summary" button ──► build() ──► SessionSummarizer (Sonnet) ──► learner.save_summary
session shutdown     ──► finish() ── build() again only if turns came after the last one
                                  └► learner.close()  (session ended_at, cost)
"""

import logging

from livekit.agents import AgentSession

from costs import CostMeter
from feedback_channel import FeedbackChannel
from learner_model import LearnerModel
from prompts import Level
from summary import SessionSummarizer, SessionSummary

log = logging.getLogger("tutor.recap")

TRANSCRIPT_ROLES = ("user", "assistant")


class SessionRecap:
    def __init__(
        self,
        session: AgentSession,
        feedback: FeedbackChannel,
        level: Level,
        learner: LearnerModel | None,
        meter: CostMeter,
    ) -> None:
        self._session = session
        self._meter = meter
        self._feedback = feedback
        self._level = level
        self._learner = learner  # None: anonymous, nothing saved
        self._summarizer = SessionSummarizer(meter=meter)
        self._turns_summarized = 0

    async def build(self) -> SessionSummary:
        history = self._feedback.history
        transcript = [
            (item.role, item.text_content)
            for item in self._session.history.items
            if getattr(item, "role", None) in TRANSCRIPT_ROLES and item.text_content
        ]
        summary = await self._summarizer.summarize(history, transcript, self._level)
        self._turns_summarized = len(history)

        if self._learner and history:
            await self._learner.save_summary(summary.model_dump_json())
        return summary

    async def finish(self) -> None:
        if not self._learner:
            return
        try:
            if len(self._feedback.history) > self._turns_summarized:
                await self.build()
        except Exception:
            log.exception("summary at shutdown failed")
        await self._learner.close(cost_json=self._meter.breakdown().model_dump_json())
