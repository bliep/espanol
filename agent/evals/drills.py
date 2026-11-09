"""Lesson modes end to end against the live agent, as a throwaway logged-in learner.

Run (needs `main.py dev` and `web` dev server running):
    cd agent && uv run python evals/drills.py

  translate_en  2 right answers (Haiku translates the prompt) + 1 wrong → scores split
  word_scores   RPC lists the drilled words
  translate_es  1 right answer → high score
  repeat        typed answer → asked to say it aloud, nothing scored;
                spoken answer (Cartesia audio published as a mic track) → Azure score
  roleplay      tutor speaks in role; feedback panel resumes
  drills        no feedback-panel messages while drilling

Cost: a few cents. Takes ~2 minutes.
"""

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import aiohttp
import anthropic
from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import (  # noqa: E402
    CHAT_TOPIC,
    FEEDBACK_TOPIC,
    create_user,
    delete_user,
    join,
    livekit_token,
)
from synth_audio import synth  # noqa: E402

from pronunciation import SAMPLE_RATE  # noqa: E402

DRILL_TOPIC = "tutor.drill"  # agent/main.py
RPC_SET_LESSON_MODE = "set_lesson_mode"
RPC_WORD_SCORES = "word_scores"
TRANSLATE_MODEL = "claude-haiku-5-5"

EVENT_TIMEOUT_S = 40  # item generation + scoring
NO_RESULT_WAIT_S = 6
FRAME_MS = 10
SILENCE_AFTER_SPEECH_S = 8  # lets VAD and the turn detector end the turn

GOOD = 80
POOR = 40
PRON_GOOD = 80


class Learner:
    """The test client: one room connection plus the streams it watches."""

    def __init__(self, room: rtc.Room, agent: str) -> None:
        self.room = room
        self.agent = agent
        self.drill: asyncio.Queue[dict] = asyncio.Queue()
        self.mode = ""  # events from an earlier mode are skipped
        self.speech: list[str] = []
        self.feedback = 0
        self._source = rtc.AudioSource(SAMPLE_RATE, 1)

    async def publish_mic(self) -> None:
        track = rtc.LocalAudioTrack.create_audio_track("mic", self._source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, options)

    async def rpc(self, method: str, payload: str = "") -> str:
        return await self.room.local_participant.perform_rpc(
            destination_identity=self.agent, method=method, payload=payload
        )

    async def set_mode(self, mode: str, **extra: str) -> None:
        self.mode = mode
        await self.rpc(RPC_SET_LESSON_MODE, json.dumps({"mode": mode, **extra}))

    async def type(self, text: str) -> None:
        await self.room.local_participant.send_text(text, topic=CHAT_TOPIC)

    async def say(self, pcm: bytes) -> None:
        """Speech then silence, in real time, as a microphone would."""
        samples = SAMPLE_RATE * FRAME_MS // 1000
        chunk = samples * 2
        audio = pcm + bytes(SAMPLE_RATE * 2 * SILENCE_AFTER_SPEECH_S)
        for i in range(0, len(audio) - chunk, chunk):
            frame = rtc.AudioFrame(audio[i : i + chunk], SAMPLE_RATE, 1, samples)
            await self._source.capture_frame(frame)

    async def next(self, kind: str) -> dict:
        while True:
            event = await asyncio.wait_for(self.drill.get(), EVENT_TIMEOUT_S)
            if event["kind"] == kind and event["mode"] == self.mode:
                return event


async def translate(client: anthropic.AsyncAnthropic, text: str, to: str) -> str:
    response = await client.messages.create(
        model=TRANSLATE_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": f"Translate into {to}. Reply with only it:\n{text}"}],
    )
    return next(b.text for b in response.content if b.type == "text").strip()


def check(results: list[tuple[str, bool]], name: str, ok: bool, detail: str) -> None:
    results.append((name, ok))
    print(f"{'✓' if ok else '✗'} {name}: {detail}")


async def run(learner: Learner, http: aiohttp.ClientSession, results: list) -> None:
    client = anthropic.AsyncAnthropic()

    # Translation EN→ES: two right, one wrong.
    await learner.set_mode("translate_en")
    scores = []
    for answer_right in (True, True, False):
        item = await learner.next("item")
        answer = (
            await translate(client, item["source"], "Spain Spanish") if answer_right else "No lo sé"
        )
        await learner.type(answer)
        result = await learner.next("result")
        scores.append(result["score"])
        print(f"   {item['source']!r} → {answer!r}: {result['score']} {result['words']}")
    ok = min(scores[:2]) >= GOOD and scores[2] <= POOR
    check(results, "translate_en scores", ok, f"{scores}")

    weakest = json.loads(await learner.rpc(RPC_WORD_SCORES))
    check(results, "word_scores RPC", len(weakest) >= 3, f"{weakest}")

    # Translation ES→EN: one right.
    await learner.set_mode("translate_es")
    item = await learner.next("item")
    await learner.type(await translate(client, item["source"], "English"))
    result = await learner.next("result")
    check(results, "translate_es score", result["score"] >= GOOD, f"{result['score']}")

    # Repeat: typed is refused, spoken is scored.
    await learner.set_mode("repeat")
    item = await learner.next("item")
    spoken_before = len(learner.speech)
    await learner.type(item["source"])
    await asyncio.sleep(NO_RESULT_WAIT_S)
    asked_aloud = any("voz alta" in s for s in learner.speech[spoken_before:])
    check(results, "repeat refuses typing", asked_aloud and learner.drill.empty(), "")

    start = time.perf_counter()
    await learner.say(await synth(http, item["source"]))
    result = await learner.next("result")
    words = [(w["word"], round(w["score"])) for w in result["words"]]
    elapsed = time.perf_counter() - start
    ok = result["score"] >= PRON_GOOD and len(words) > 3
    check(results, "repeat spoken score", ok, f"{result['score']} {words} ({elapsed:.0f} s)")
    feedback_in_drills = learner.feedback

    # Role-play: tutor speaks in role, feedback panel is back.
    await learner.set_mode("roleplay", scenario="doctor")
    await asyncio.sleep(NO_RESULT_WAIT_S)
    await learner.type("Me duele mucho la cabeza desde ayer.")
    await asyncio.sleep(NO_RESULT_WAIT_S)
    check(results, "no feedback during drills", feedback_in_drills == 0, f"{feedback_in_drills}")
    check(results, "roleplay feedback resumes", learner.feedback > 0, f"{learner.speech[-2:]}")


async def main() -> int:
    results: list[tuple[str, bool]] = []
    async with aiohttp.ClientSession() as http:
        user_id, access_token = await create_user(http)
        try:
            _, details = await livekit_token(http, access_token)
            room = rtc.Room()
            learner: Learner | None = None

            def on_drill(reader: rtc.TextStreamReader, _sender: str) -> None:
                asyncio.ensure_future(_put(reader, learner.drill.put_nowait))

            def on_feedback(_reader: rtc.TextStreamReader, _sender: str) -> None:
                learner.feedback += 1

            room.register_text_stream_handler(DRILL_TOPIC, on_drill)
            room.register_text_stream_handler(FEEDBACK_TOPIC, on_feedback)
            speech: list[str] = []
            room, agent = await join(details, room, speech.append)
            learner = Learner(room, agent)
            learner.speech = speech
            await learner.publish_mic()

            await run(learner, http, results)
            await room.disconnect()
        finally:
            await delete_user(http, user_id)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks pass")
    return 1 if failed else 0


async def _put(reader: rtc.TextStreamReader, put: Callable[[dict], None]) -> None:
    put(json.loads(await reader.read_all()))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
