"""Opt-in recording of the learner's spoken turns for the eval set (RECORD_TURNS=1).

  conversation agents' stt_node ──► tee() ──► SpeechCapture (16 kHz PCM)
  user turn committed ──► <time>.wav + <time>.json in agent/evals/recordings/
      json: {"heard": Deepgram's live transcript, "reference": same (correct it by hand),
             "expected_corrections": [], "level": "A2"}

Typed turns have no audio and are skipped. Recordings are personal data: they stay
on this machine (the folder is git-ignored) and are only made when the flag is set.
"""

import json
import logging
import os
import wave
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from pathlib import Path

from livekit import rtc
from livekit.agents import AgentSession, ConversationItemAddedEvent

from prompts import Level
from pronunciation import SAMPLE_RATE
from speech_capture import SpeechCapture

log = logging.getLogger("tutor.recorder")

RECORD_ENV = "RECORD_TURNS"
RECORDINGS = Path(__file__).parent / "evals" / "recordings"
USER_ROLE = "user"
SAMPLE_WIDTH = 2  # 16-bit


def recording_enabled() -> bool:
    return os.environ.get(RECORD_ENV) == "1"


class TurnRecorder:
    def __init__(self, level: Level, folder: Path = RECORDINGS) -> None:
        self._level = level
        self._folder = folder
        self._capture = SpeechCapture()

    def attach(self, session: AgentSession) -> None:
        self._capture.attach(session)
        session.on("conversation_item_added", self._on_item)
        log.info("recording learner turns to %s", self._folder)

    async def tee(self, audio: AsyncIterable[rtc.AudioFrame]) -> AsyncIterator[rtc.AudioFrame]:
        """Passes STT input through while capturing it."""
        async for frame in audio:
            self._capture.push(frame)
            yield frame

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        if ev.item.role != USER_ROLE:
            return
        pcm = self._capture.take()
        heard = (getattr(ev.item, "text_content", None) or "").strip()
        if not pcm or not heard:
            return  # typed turn, or nothing recognised

        self._folder.mkdir(parents=True, exist_ok=True)
        stem = self._folder / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with wave.open(str(stem.with_suffix(".wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)

        meta = {
            "heard": heard,
            "reference": heard,
            "expected_corrections": [],
            "level": self._level.value,
        }
        stem.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log.info("recorded turn %s: %s", stem.name, heard)
