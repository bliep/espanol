"""Tutor agent entrypoint.

Run: uv run python main.py console   (terminal mic/speaker, no web client)
     uv run python main.py dev       (joins LiveKit Cloud rooms)
"""

import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, room_io

import pipeline
from costs import CostMeter
from drill_content import DrillItemGenerator, TranslationJudge
from drills import DrillDeps
from feedback import FeedbackExtractor
from feedback_channel import FeedbackChannel, FeedbackMessage
from input_modes import InputController, InputMode
from latency import LatencyLogger
from learner_model import EMPTY_LESSON, LearnerModel, LessonContext
from learner_store import LearnerStore
from mode_controller import ModeController
from modes import ModeRequest
from prompts import DEFAULT_LEVEL, Level, build_greeting
from pronunciation import PronunciationAssessor
from sentence_translator import SentenceTranslator
from session_recap import SessionRecap
from turn_recorder import TurnRecorder, recording_enabled
from tutor_agents import Tutor

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
RPC_SET_LESSON_MODE = "set_lesson_mode"  # payload: ModeRequest JSON
RPC_WORD_SCORES = "word_scores"  # returns WeakWord JSON list
RPC_SESSION_COST = "session_cost"  # returns CostBreakdown JSON
RPC_REPLAY_SLOW = "replay_slow"  # payload: tutor line to re-speak slowly
RPC_TRANSLATE = "translate_line"  # payload: tutor line; returns English
RPC_OK = "ok"

DB_URL_KEY = "SUPABASE_DB_URL"
AZURE_KEY_ENV = "AZURE_SPEECH_KEY"  # repeat-after-me only; sessions run without it

# Text stream topics (web/lib/tutor-feedback.ts, web/lib/tutor-drill.ts).
FEEDBACK_TOPIC = "tutor.feedback"
DRILL_TOPIC = "tutor.drill"

log = logging.getLogger("tutor")


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


def register_mode_rpcs(
    participant: rtc.LocalParticipant, modes: ModeController, learner: LearnerModel | None
) -> None:
    """Expose lesson mode switching and drill word scores to the web client."""

    async def set_mode(data: rtc.RpcInvocationData) -> str:
        try:
            modes.set_mode(ModeRequest.model_validate_json(data.payload))
        except ValueError as error:
            raise rtc.RpcError(rtc.RpcError.ErrorCode.APPLICATION_ERROR, str(error)) from None
        return RPC_OK

    async def word_scores(_: rtc.RpcInvocationData) -> str:
        weakest = await learner.weakest_words() if learner else []
        return json.dumps([w.model_dump() for w in weakest])

    participant.register_rpc_method(RPC_SET_LESSON_MODE, set_mode)
    participant.register_rpc_method(RPC_WORD_SCORES, word_scores)


def register_support_rpcs(
    participant: rtc.LocalParticipant, session: AgentSession, meter: CostMeter
) -> None:
    """Listening support (slow replay, translation) and the running session cost."""
    slow_tts = pipeline.build_slow_tts()
    translator = SentenceTranslator(meter=meter)

    def line_of(data: rtc.RpcInvocationData) -> str:
        if not data.payload.strip():
            raise rtc.RpcError(rtc.RpcError.ErrorCode.APPLICATION_ERROR, "empty line")
        return data.payload.strip()

    async def replay(data: rtc.RpcInvocationData) -> str:
        line = line_of(data)
        meter.add_tts(len(line))

        async def frames() -> AsyncIterator[rtc.AudioFrame]:
            async with slow_tts.synthesize(line) as stream:
                async for chunk in stream:
                    yield chunk.frame

        # No text: the transcript and chat history keep the original line only.
        session.say("", audio=frames(), add_to_chat_ctx=False)
        return RPC_OK

    async def translate(data: rtc.RpcInvocationData) -> str:
        return await translator.translate(line_of(data))

    async def cost(_: rtc.RpcInvocationData) -> str:
        return meter.breakdown().model_dump_json()

    participant.register_rpc_method(RPC_REPLAY_SLOW, replay)
    participant.register_rpc_method(RPC_TRANSLATE, translate)
    participant.register_rpc_method(RPC_SESSION_COST, cost)


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
    meter = CostMeter()
    meter.attach(session)

    async def publish_feedback(payload: str) -> None:
        await ctx.room.local_participant.send_text(payload, topic=FEEDBACK_TOPIC)

    async def record_feedback(message: FeedbackMessage) -> None:
        await learner.record_turn(message.learner, message.feedback)

    feedback = FeedbackChannel(
        FeedbackExtractor(meter=meter),
        level,
        publish_feedback,
        record_feedback if learner else None,
    )
    feedback.attach(session)

    recap = SessionRecap(session, feedback, level, learner, meter)

    # One callback: LiveKit runs shutdown callbacks concurrently; the store must outlive recap.
    async def shutdown() -> None:
        await recap.finish()
        if store:
            await store.close()

    ctx.add_shutdown_callback(shutdown)

    async def publish_drill(payload: str) -> None:
        await ctx.room.local_participant.send_text(payload, topic=DRILL_TOPIC)

    deps = DrillDeps(
        level,
        lesson,
        learner,
        publish_drill,
        generator=DrillItemGenerator(meter=meter),
        judge=TranslationJudge(meter=meter),
        assessor=PronunciationAssessor(meter=meter) if os.environ.get(AZURE_KEY_ENV) else None,
    )
    recorder = TurnRecorder(level) if recording_enabled() else None
    if recorder:
        recorder.attach(session)
    modes = ModeController(session, feedback, deps, recorder)

    # RPCs before start: the greeting tells clients the agent is ready for them.
    participant = ctx.room.local_participant
    register_input_rpcs(participant, InputController(session))
    register_lesson_rpcs(participant, recap, lesson)
    register_mode_rpcs(participant, modes, learner)
    register_support_rpcs(participant, session, meter)

    greeting = build_greeting([w.spanish for w in lesson.due_words])
    text_input = room_io.TextInputOptions(text_input_cb=modes.on_text_input)
    await session.start(
        room=ctx.room,
        agent=Tutor(level, lesson, greeting, recorder),
        room_options=room_io.RoomOptions(text_input=text_input),
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
