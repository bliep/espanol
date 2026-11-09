"""Spoken conversation turns against the live agent: drives load for latency, reports cost.

Run (needs `main.py dev` and `web` dev server running):
    cd agent && uv run python evals/voice_latency.py

  synthetic learner sentence on a mic track ──► agent replies ──► next sentence

The authoritative latency is the agent's own "tutor.latency - e2e" log line per reply
(LiveKit's e2e_latency: end of user speech ──► agent starts speaking; target ~800 ms):
local `main.py dev` output, or `lk agent logs` for the deployed agent.

The client-side estimate printed here (end of our audio ──► first tutor transcript
chunk) is rough: it misattributes late chunks of a previous reply and reads too low.

Cost: a few cents. Takes ~1.5 minutes.
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lesson_modes import Learner  # noqa: E402
from memory import create_user, delete_user, join, livekit_token  # noqa: E402
from synth_audio import synth  # noqa: E402

from pronunciation import SAMPLE_RATE  # noqa: E402

TARGET_MS = 800
RPC_SESSION_COST = "session_cost"  # agent/main.py
BYTES_PER_SECOND = SAMPLE_RATE * 2
TURNS = (
    "Hola Lucía, hoy estoy un poco cansado.",
    "Ayer fui al cine con mis amigos.",
    "Me gusta mucho la comida española.",
    "Este fin de semana quiero ir a la playa.",
    "Trabajo en una oficina en Ámsterdam.",
    "Mi hermana vive en Utrecht con su familia.",
)


async def main() -> int:
    starts: list[float] = []  # perf_counter of each tutor utterance start
    latencies: list[float] = []

    async with aiohttp.ClientSession() as http:
        audio = await asyncio.gather(*(synth(http, t) for t in TURNS))
        user_id, access_token = await create_user(http)
        try:
            _, details = await livekit_token(http, access_token)
            room, agent = await join(
                details, on_speech_start=lambda: starts.append(time.perf_counter())
            )
            learner = Learner(room, agent)
            await learner.publish_mic()
            await asyncio.sleep(3)  # greeting finishes

            for text, pcm in zip(TURNS, audio, strict=True):
                begin = time.perf_counter()
                speech_end = begin + len(pcm) / BYTES_PER_SECOND
                await learner.say(pcm)  # speech, then silence while the tutor answers
                replies = [s for s in starts if s > speech_end]
                if not replies:
                    print(f"  no reply: {text}")
                    continue
                latencies.append((replies[0] - speech_end) * 1000)
                print(f"{latencies[-1]:5.0f} ms | {text}")

            cost = await learner.rpc(RPC_SESSION_COST)
            print(f"session cost: {cost}")
            await room.disconnect()
        finally:
            await delete_user(http, user_id)

    if not latencies:
        return 1
    ordered = sorted(latencies)
    p90 = ordered[int(0.9 * (len(ordered) - 1))]
    median = statistics.median(latencies)
    print(f"\nclient-side estimate (rough): median {median:.0f} ms, p90 {p90:.0f} ms")
    print("authoritative: the agent's 'tutor.latency - e2e' log lines")
    return 0 if median <= TARGET_MS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
