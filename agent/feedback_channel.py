"""Runs the feedback extractor beside the conversation and publishes the results.

  conversation_item_added (user) ──► background task ──► FeedbackExtractor (Haiku)
                                                              │
                         publish(json) ◄──────────────────────┘  e.g. room text stream
                         history       ◄── kept for the session summary
                         record(msg)   ◄── e.g. learner model (vocab, error log)

The tutor's reply path never awaits any of this.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from livekit.agents import AgentSession, ConversationItemAddedEvent
from pydantic import BaseModel

from feedback import Feedback, FeedbackExtractor
from prompts import Level

log = logging.getLogger("tutor.feedback")

Publish = Callable[[str], Awaitable[None]]
Record = Callable[["FeedbackMessage"], Awaitable[None]]

USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"


class FeedbackMessage(BaseModel):
    """Wire format on the feedback text stream (mirrored in web/lib/tutor-feedback.ts)."""

    turn_id: str
    learner: str
    feedback: Feedback


class FeedbackChannel:
    def __init__(
        self,
        extractor: FeedbackExtractor,
        level: Level,
        publish: Publish,
        record: Record | None = None,
    ) -> None:
        self._extractor = extractor
        self._level = level
        self._publish = publish
        self._record = record
        self._tutor_prev = ""
        self._history: list[FeedbackMessage] = []
        self._tasks: set[asyncio.Task[None]] = set()  # strong refs until done

    @property
    def history(self) -> list[FeedbackMessage]:
        return list(self._history)

    def attach(self, session: AgentSession) -> None:
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return

        if item.role == ASSISTANT_ROLE:
            self._tutor_prev = text
            return
        if item.role != USER_ROLE:
            return

        task = asyncio.create_task(self._run(item.id, text, self._tutor_prev))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, turn_id: str, learner: str, tutor_prev: str) -> None:
        start = time.perf_counter()
        try:
            fb = await self._extractor.extract(learner, tutor_prev, self._level)
            message = FeedbackMessage(turn_id=turn_id, learner=learner, feedback=fb)
            self._history.append(message)
            await self._publish(message.model_dump_json())
        except Exception:
            log.exception("feedback failed for turn %s", turn_id)
            return

        log.info(
            "feedback %s | %4.0f ms | %d corrections, %d vocab",
            turn_id,
            (time.perf_counter() - start) * 1000,
            len(fb.corrections),
            len(fb.new_vocab),
        )

        # After publishing: storage never delays the panel.
        if not self._record:
            return
        try:
            await self._record(message)
        except Exception:
            log.exception("recording feedback failed for turn %s", turn_id)
