"""Long typed chat against the live agent: stability, latency drift, recasts.

Run (agent must be running: uv run python main.py dev):
    cd agent && uv run python evals/long_chat.py

  this script ──lk.chat──► LiveKit room ──► tutor agent (full pipeline incl. TTS)
              ◄─lk.transcription─────────────┘

Reply time = typed turn sent → first transcription text, i.e. LLM first token plus
TTS first audio; no STT or turn detection on the typed path. Voice latency is in the
agent's own `turn … total … ms` log lines.
"""

import asyncio
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

AGENT_NAME = "tutor"  # agent/main.py
CHAT_TOPIC = "lk.chat"
TRANSCRIPTION_TOPIC = "lk.transcription"
REPLY_TIMEOUT_S = 30
JOIN_TIMEOUT_S = 30
# Typed path, as the client sees it. Agent side is ~0.85 s (LLM ~0.75 + TTS ~0.1); the
# rest is network and transcript sync to audio playout. Measured median 1.75–1.8 s
# (2026-11-08); accepted for Phase 1.
TARGET_MEDIAN_MS = 2000

# A2 learner: correct Spanish, mistakes, English and mixed turns, like a real chat.
LEARNER_TURNS = (
    "Hola, estoy bien, gracias.",
    "Me llamo Anuj. Vivo en Ámsterdam.",
    "Yo trabajo como ingeniero.",
    "I work with computers all day.",
    "Trabajo con ordenadores todo el día.",
    "Me gusta mucho las montañas.",
    "El verano pasado fui a los Pirineos.",
    "We walked a lot and the food was amazing.",
    "Caminamos mucho y la comida era increíble.",
    "Comí mucho... how do you say... cheese.",
    "Sí, queso. Me encanta el queso manchego.",
    "Mi hermana is a teacher en Utrecht.",
    "Ella enseña matemáticas.",
    "Yo es muy cansado hoy.",
    "Porque trabajé mucho ayer.",
    "How do you say 'weekend' in Spanish?",
    "El fin de semana voy a cocinar paella.",
    "No sé cocinar bien, pero me gusta.",
    "Please, can we speak English for a moment?",
    "Vale, vale. Hablo español.",
    "Mañana voy al supermercado.",
    "Necesito comprar arroz y... chicken.",
    "Pollo, sí. Y también verduras.",
    "Mi verdura favorita es la pimiento.",
    "Tengo dos gatos en casa.",
    "They are very lazy.",
    "Son muy perezosos.",
    "Duermen todo el día en el sofá.",
    "Me gustaría viajar a Sevilla en primavera.",
    "Muchas gracias, Lucía. Hasta luego.",
)


def _token(room: str) -> str:
    return (
        api.AccessToken()
        .with_identity(f"long-chat-{uuid.uuid4().hex[:4]}")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)])
        )
        .to_jwt()
    )


async def _wait_for_agent(room: rtc.Room) -> str:
    deadline = time.monotonic() + JOIN_TIMEOUT_S
    while time.monotonic() < deadline:
        for p in room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                return p.identity
        await asyncio.sleep(0.5)
    raise TimeoutError("agent did not join; is `main.py dev` running?")


async def main() -> int:
    room = rtc.Room()
    replies: asyncio.Queue[tuple[float, str]] = asyncio.Queue()
    agent_identity = ""

    # Each agent utterance arrives as one text stream; record when it starts, then its text.
    def on_transcription(reader: rtc.TextStreamReader, sender: str) -> None:
        if sender != agent_identity:
            return
        started = time.perf_counter()

        async def read() -> None:
            await replies.put((started, await reader.read_all()))

        asyncio.ensure_future(read())

    room.register_text_stream_handler(TRANSCRIPTION_TOPIC, on_transcription)
    await room.connect(os.environ["LIVEKIT_URL"], _token(f"long-chat-{uuid.uuid4().hex[:6]}"))
    agent_identity = await _wait_for_agent(room)

    # Greeting first, so it doesn't count as a reply.
    _, greeting = await asyncio.wait_for(replies.get(), REPLY_TIMEOUT_S)
    print(f"tutor: {greeting}\n")

    times_ms: list[float] = []
    session_start = time.perf_counter()
    for i, turn in enumerate(LEARNER_TURNS, 1):
        sent = time.perf_counter()
        await room.local_participant.send_text(turn, topic=CHAT_TOPIC)
        try:
            started, text = await asyncio.wait_for(replies.get(), REPLY_TIMEOUT_S)
        except TimeoutError:
            print(f"{i:2d} NO REPLY within {REPLY_TIMEOUT_S}s to: {turn}")
            return 1

        times_ms.append((started - sent) * 1000)
        print(f"{i:2d} {times_ms[-1]:5.0f} ms | {turn}\n          → {text}")

    await room.disconnect()

    minutes = (time.perf_counter() - session_start) / 60
    first, last = times_ms[:10], times_ms[-10:]
    median = statistics.median(times_ms)
    print(
        f"\n{len(times_ms)} turns in {minutes:.1f} min | median {median:.0f} ms "
        f"(target {TARGET_MEDIAN_MS}) | first 10 {statistics.median(first):.0f} ms, "
        f"last 10 {statistics.median(last):.0f} ms"
    )
    return 0 if median <= TARGET_MEDIAN_MS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
