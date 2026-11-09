"""Tutor agent entrypoint.

Run: uv run python main.py console   (terminal mic/speaker, no web client)
     uv run python main.py dev       (joins LiveKit Cloud rooms)
"""

import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, JobContext, JobProcess

import pipeline
from feedback import FeedbackExtractor
from feedback_channel import FeedbackChannel, FeedbackMessage
from input_modes import InputController, InputMode
from latency import LatencyLogger
from learner_model import EMPTY_LESSON, LearnerModel, LessonContext
from learner_store import LearnerStore
from prompts import DEFAULT_LEVEL, Level, build_greeting, build_instructions
from session_recap import SessionRecap

load_dotenv(Path(__file__).parent.parent / ".env")

VAD_KEY = "vad"
LEVEL_KEY = "level"

# RPC methods the web client calls on the agent participant.
RPC_SET_INPUT_MODE = "set_input_mode"  # payload: "open" | "ptt"
RPC_PTT_START = "ptt_start"
RPC_PTT_END = "ptt_end"
RPC_PTT_CANCEL = "ptt_cancel"
RPC_SESSION_SUMMARY = "session_summary"  # returns SessionSummary JSON
RPC_LESSON_CONTEXT = "lesson_context"  # returns LessonContext JSON
RPC_OK = "ok"

DB_URL_KEY = "SUPABASE_DB_URL"

# Text stream topic for per-turn feedback (web/lib/tutor-feedback.ts).
FEEDBACK_TOPIC = "tutor.feedback"

log = logging.getLogger("tutor")


class Tutor(Agent):
    def __init__(self, level: Level, lesson: LessonContext) -> None:
        words = [(w.spanish, w.english) for w in lesson.due_words]
        errors = [(e.category.value, e.original, e.corrected) for e in lesson.recent_errors]
        super().__init__(instructions=build_instructions(level, words, errors))


def prewarm(proc: JobProcess) -> None:
    proc.userdata[VAD_KEY] = pipeline.load_vad()


def read_level(metadata: str) -> Level:
    """Level from dispatch metadata, e.g. '{"level": "B1"}'; default if absent/invalid."""
    try:
        return Level(json.loads(metadata)[LEVEL_KEY])
    except (ValueError, KeyError, TypeError):
        return DEFAULT_LEVEL


def register_input_rpcs(participant: rtc.LocalParticipant, inputs: InputController) -> None:
    """Expose open-mic / push-to-talk controls to the web client."""

    async def set_mode(data: rtc.RpcInvocationData) -> str:
        try:
            inputs.set_mode(InputMode(data.payload))
        except ValueError:
            raise rtc.RpcError(
                rtc.RpcError.ErrorCode.APPLICATION_ERROR, f"unknown mode {data.payload!r}"
            ) from None
        return RPC_OK

    def handler(action: Callable[[], None]) -> Callable[[rtc.RpcInvocationData], Awaitable[str]]:
        async def run(_: rtc.RpcInvocationData) -> str:
            action()
            return RPC_OK

        return run

    participant.register_rpc_method(RPC_SET_INPUT_MODE, set_mode)
    participant.register_rpc_method(RPC_PTT_START, handler(inputs.start_turn))
    participant.register_rpc_method(RPC_PTT_END, handler(inputs.end_turn))
    participant.register_rpc_method(RPC_PTT_CANCEL, handler(inputs.cancel_turn))


def register_lesson_rpcs(
    participant: rtc.LocalParticipant, recap: SessionRecap, lesson: LessonContext
) -> None:
    """Expose the session opener and the end-of-session summary to the web client."""

    async def summarize(_: rtc.RpcInvocationData) -> str:
        return (await recap.build()).model_dump_json()

    async def opener(_: rtc.RpcInvocationData) -> str:
        return lesson.model_dump_json()

    participant.register_rpc_method(RPC_SESSION_SUMMARY, summarize)
    participant.register_rpc_method(RPC_LESSON_CONTEXT, opener)


async def open_learner(ctx: JobContext, level: Level) -> tuple[LearnerModel, LearnerStore] | None:
    """Learner model when the participant is logged in (identity = Supabase user id).

    None (no memory, nothing saved) in console mode, for anonymous identities such as
    evals/long_chat.py, without SUPABASE_DB_URL, or if the database is unreachable.
    """
    if ctx.is_fake_job():
        return None

    participant = await ctx.wait_for_participant()
    try:
        learner_id = UUID(participant.identity)
    except ValueError:
        log.info("anonymous participant %s: no learner memory", participant.identity)
        return None

    dsn = os.environ.get(DB_URL_KEY)
    if not dsn:
        log.warning("%s not set: no learner memory", DB_URL_KEY)
        return None

    try:
        store = await LearnerStore.connect(dsn)
    except Exception:
        log.exception("learner store unreachable: no learner memory")
        return None

    try:
        return await LearnerModel.open(store, learner_id, level), store
    except Exception:
        log.exception("opening learner %s failed: no learner memory", learner_id)
        await store.close()
        return None


server = AgentServer(setup_fnc=prewarm)


@server.rtc_session(agent_name="tutor")
async def entrypoint(ctx: JobContext) -> None:
    level = read_level(ctx.job.metadata)
    log.info("session level %s", level)

    await ctx.connect()
    learner, store = await open_learner(ctx, level) or (None, None)
    lesson = await learner.lesson() if learner else EMPTY_LESSON
    log.info(
        "lesson: %d due words, %d error types", len(lesson.due_words), len(lesson.recent_errors)
    )

    session = pipeline.build_session(ctx.proc.userdata[VAD_KEY], level, lesson.keyterms())
    LatencyLogger().attach(session)

    async def publish_feedback(payload: str) -> None:
        await ctx.room.local_participant.send_text(payload, topic=FEEDBACK_TOPIC)

    async def record_feedback(message: FeedbackMessage) -> None:
        await learner.record_turn(message.learner, message.feedback)

    feedback = FeedbackChannel(
        FeedbackExtractor(), level, publish_feedback, record_feedback if learner else None
    )
    feedback.attach(session)

    recap = SessionRecap(session, feedback, level, learner)

    # One callback: LiveKit runs shutdown callbacks concurrently; the store must outlive recap.
    async def shutdown() -> None:
        await recap.finish()
        if store:
            await store.close()

    ctx.add_shutdown_callback(shutdown)

    await session.start(room=ctx.room, agent=Tutor(level, lesson))
    register_input_rpcs(ctx.room.local_participant, InputController(session))
    register_lesson_rpcs(ctx.room.local_participant, recap, lesson)

    words = [w.spanish for w in lesson.due_words]
    await session.generate_reply(instructions=build_greeting(words))


if __name__ == "__main__":
    agents.cli.run_app(server)
