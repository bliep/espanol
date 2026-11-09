"""Learner memory end to end: a logged-in learner's next session opens with their
due words and recent mistakes (Phase 3 "done when").

Run (needs `main.py dev` and `web` dev server running):
    cd agent && uv run python evals/memory.py

  throwaway Supabase user ──► web /api/token (401 without login, token with) ──► room
  session 1: a vocab gap + a ser/estar error ──► disconnect (summary saved at shutdown)
  wait for the FSRS learning step ──► session 2: lesson_context RPC must list both
  user deleted at the end (learner rows cascade)

Cost: a few cents (tutor replies, feedback, one summary). Takes ~2 minutes.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from livekit import rtc

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

WEB_URL = os.environ.get("WEB_URL", "http://localhost:3000")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
PUBLISHABLE_KEY = os.environ["SUPABASE_PUBLISHABLE_KEY"]

AGENT_NAME = "tutor"
CHAT_TOPIC = "lk.chat"
TRANSCRIPTION_TOPIC = "lk.transcription"
FEEDBACK_TOPIC = "tutor.feedback"  # agent/main.py
RPC_LESSON_CONTEXT = "lesson_context"  # agent/main.py

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401

JOIN_TIMEOUT_S = 30
FEEDBACK_TIMEOUT_S = 20
FSRS_LEARNING_STEP_S = 65  # a missed new word is due again after ~1 min
SHUTDOWN_WAIT_S = 20  # agent saves the summary after the room empties

GAP_TURN = "Hoy llevo un... umbrella porque llueve."
ERROR_TURN = "Yo es muy cansado hoy."
EXPECTED_WORD = "paraguas"
EXPECTED_CATEGORY = "ser_estar"


async def create_user(http: aiohttp.ClientSession) -> tuple[str, str]:
    """Confirmed throwaway user; returns (user id, access token)."""
    email = f"memory-eval-{uuid.uuid4().hex[:8]}@example.com"
    password = uuid.uuid4().hex
    admin = {"apikey": SECRET_KEY, "Authorization": f"Bearer {SECRET_KEY}"}

    async with http.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=admin,
        json={"email": email, "password": password, "email_confirm": True},
    ) as r:
        r.raise_for_status()
        user_id = (await r.json())["id"]

    async with http.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": PUBLISHABLE_KEY},
        json={"email": email, "password": password},
    ) as r:
        r.raise_for_status()
        return user_id, (await r.json())["access_token"]


async def delete_user(http: aiohttp.ClientSession, user_id: str) -> None:
    admin = {"apikey": SECRET_KEY, "Authorization": f"Bearer {SECRET_KEY}"}
    async with http.delete(f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}", headers=admin) as r:
        r.raise_for_status()


async def saved_sessions(http: aiohttp.ClientSession, user_id: str) -> list[dict]:
    """The learner's session rows (secret key bypasses row-level security)."""
    admin = {"apikey": SECRET_KEY, "Authorization": f"Bearer {SECRET_KEY}"}
    params = {"learner_id": f"eq.{user_id}", "select": "ended_at,summary", "order": "started_at"}
    async with http.get(f"{SUPABASE_URL}/rest/v1/sessions", headers=admin, params=params) as r:
        r.raise_for_status()
        return await r.json()


async def livekit_token(http: aiohttp.ClientSession, access_token: str | None) -> tuple[int, dict]:
    """Calls the web token route as the browser does; returns (status, body)."""
    body = {"room_config": {"agents": [{"agent_name": AGENT_NAME}]}}
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    async with http.post(f"{WEB_URL}/api/token", json=body, headers=headers) as r:
        return r.status, (await r.json() if r.status == HTTP_OK else {})


async def join(
    details: dict,
    room: rtc.Room | None = None,
    on_speech: Callable[[str], None] | None = None,
) -> tuple[rtc.Room, str]:
    """Connects and waits for the tutor's greeting: only then are its RPCs and chat live.

    on_speech: called with the text of each tutor utterance.
    """
    room = room or rtc.Room()
    greeted = asyncio.Event()

    def on_transcription(reader: rtc.TextStreamReader, _sender: str) -> None:
        greeted.set()
        if on_speech:
            asyncio.ensure_future(_read_into(reader, on_speech))

    room.register_text_stream_handler(TRANSCRIPTION_TOPIC, on_transcription)
    await room.connect(details["serverUrl"], details["participantToken"])

    deadline = time.monotonic() + JOIN_TIMEOUT_S
    while time.monotonic() < deadline:
        for p in room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                await asyncio.wait_for(greeted.wait(), JOIN_TIMEOUT_S)
                return room, p.identity
        await asyncio.sleep(0.5)
    raise TimeoutError("agent did not join; is `main.py dev` running?")


async def _read_into(reader: rtc.TextStreamReader, callback: Callable[[str], None]) -> None:
    try:
        callback(await reader.read_all())
    except rtc.StreamError:
        pass  # utterance cut off by our own disconnect


async def first_session(details: dict) -> None:
    room = rtc.Room()
    feedback: asyncio.Queue[dict] = asyncio.Queue()

    def on_feedback(reader: rtc.TextStreamReader, _sender: str) -> None:
        async def read() -> None:
            await feedback.put(json.loads(await reader.read_all()))

        asyncio.ensure_future(read())

    room.register_text_stream_handler(FEEDBACK_TOPIC, on_feedback)
    await join(details, room)

    for turn in (GAP_TURN, ERROR_TURN):
        await room.local_participant.send_text(turn, topic=CHAT_TOPIC)
        message = await asyncio.wait_for(feedback.get(), FEEDBACK_TIMEOUT_S)
        print(f"  feedback: {json.dumps(message['feedback'], ensure_ascii=False)}")

    await asyncio.sleep(2)  # let the agent store the last turn
    await room.disconnect()


async def lesson_of(details: dict) -> dict:
    room, agent = await join(details)
    lesson = json.loads(
        await room.local_participant.perform_rpc(
            destination_identity=agent, method=RPC_LESSON_CONTEXT, payload=""
        )
    )
    await room.disconnect()
    return lesson


async def main() -> int:
    async with aiohttp.ClientSession() as http:
        status, _ = await livekit_token(http, None)
        print(f"token without login: {status} (want {HTTP_UNAUTHORIZED})")
        if status != HTTP_UNAUTHORIZED:
            return 1

        user_id, access_token = await create_user(http)
        try:
            status, details = await livekit_token(http, access_token)
            print(f"token with login: {status} (want {HTTP_OK})")
            if status != HTTP_OK:
                return 1

            print("session 1")
            await first_session(details)

            print(f"waiting {FSRS_LEARNING_STEP_S}s for the FSRS learning step")
            await asyncio.sleep(FSRS_LEARNING_STEP_S)

            print("session 2")
            _, details = await livekit_token(http, access_token)
            lesson = await lesson_of(details)
            print(f"  lesson: {json.dumps(lesson, ensure_ascii=False)}")

            await asyncio.sleep(SHUTDOWN_WAIT_S)  # let session 2 close before checking
            first = (await saved_sessions(http, user_id))[0]
            summary = first["summary"] or {}
            print(f"  session 1 saved: ended {first['ended_at']}, summary {summary}")

            words = " ".join(w["spanish"] for w in lesson["due_words"])
            categories = [e["category"] for e in lesson["recent_errors"]]
            ok = (
                EXPECTED_WORD in words
                and EXPECTED_CATEGORY in categories
                and first["ended_at"] is not None
                and bool(summary.get("patterns"))
            )
            print(f"\n{'PASS' if ok else 'FAIL'}: due words {words!r}, errors {categories}")
            return 0 if ok else 1
        finally:
            await delete_user(http, user_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
