"""Tutor agent entrypoint.

Run: uv run python main.py console   (terminal mic/speaker, no web client)
     uv run python main.py dev       (joins LiveKit Cloud rooms)
"""

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, JobContext, JobProcess

import pipeline
from input_modes import InputController, InputMode
from latency import LatencyLogger
from prompts import DEFAULT_LEVEL, GREETING, Level, build_instructions

load_dotenv(Path(__file__).parent.parent / ".env")

VAD_KEY = "vad"
LEVEL_KEY = "level"

# RPC methods the web client calls on the agent participant.
RPC_SET_INPUT_MODE = "set_input_mode"  # payload: "open" | "ptt"
RPC_PTT_START = "ptt_start"
RPC_PTT_END = "ptt_end"
RPC_PTT_CANCEL = "ptt_cancel"
RPC_OK = "ok"

log = logging.getLogger("tutor")


class Tutor(Agent):
    def __init__(self, level: Level) -> None:
        super().__init__(instructions=build_instructions(level))


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


server = AgentServer(setup_fnc=prewarm)


@server.rtc_session(agent_name="tutor")
async def entrypoint(ctx: JobContext) -> None:
    level = read_level(ctx.job.metadata)
    log.info("session level %s", level)

    session = pipeline.build_session(ctx.proc.userdata[VAD_KEY], level)
    LatencyLogger().attach(session)

    await session.start(room=ctx.room, agent=Tutor(level))
    register_input_rpcs(ctx.room.local_participant, InputController(session))

    await session.generate_reply(instructions=GREETING)


if __name__ == "__main__":
    agents.cli.run_app(server)
