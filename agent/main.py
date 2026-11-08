"""Tutor agent entrypoint.

Run: uv run python main.py console   (terminal mic/speaker, no web client)
     uv run python main.py dev       (joins LiveKit Cloud rooms)
"""

from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, JobContext, JobProcess

import pipeline
from latency import LatencyLogger
from prompts import GREETING, TUTOR_INSTRUCTIONS

load_dotenv(Path(__file__).parent.parent / ".env")

VAD_KEY = "vad"


class Tutor(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=TUTOR_INSTRUCTIONS)


def prewarm(proc: JobProcess) -> None:
    proc.userdata[VAD_KEY] = pipeline.load_vad()


server = AgentServer(setup_fnc=prewarm)


@server.rtc_session(agent_name="tutor")
async def entrypoint(ctx: JobContext) -> None:
    session = pipeline.build_session(ctx.proc.userdata[VAD_KEY])
    LatencyLogger().attach(session)

    await session.start(room=ctx.room, agent=Tutor())
    await session.generate_reply(instructions=GREETING)


if __name__ == "__main__":
    agents.cli.run_app(server)
