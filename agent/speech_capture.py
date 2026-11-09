"""Captures the learner's spoken answer as 16 kHz PCM for pronunciation scoring.

  stt_node frames ──► push() ──► ring buffer (last ~1 s, always)
  user starts speaking ──► recording starts with the ring contents (no clipped onset)
  ... pauses inside the answer keep recording ...
  take() at end of turn ──► PCM, or None if the learner never spoke (typed answer)

Driven by the session's user_state_changed events; transport-agnostic otherwise.
"""

from collections import deque

from livekit import rtc
from livekit.agents import AgentSession, UserStateChangedEvent

from pronunciation import SAMPLE_RATE

PRE_ROLL_FRAMES = 50  # ~1 s of 20 ms frames before speech was detected
MAX_SECONDS = 30  # cap: an answer is one sentence
BYTES_PER_SAMPLE = 2
SPEAKING = "speaking"


class SpeechCapture:
    def __init__(self) -> None:
        self._ring: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
        self._recording: list[bytes] | None = None
        self._resampler: rtc.AudioResampler | None = None
        self._input_rate = 0

    def attach(self, session: AgentSession) -> None:
        session.on("user_state_changed", self._on_user_state)

    def detach(self, session: AgentSession) -> None:
        session.off("user_state_changed", self._on_user_state)

    def push(self, frame: rtc.AudioFrame) -> None:
        for chunk in self._resample(frame):
            if self._recording is None:
                self._ring.append(chunk)
            elif sum(map(len, self._recording)) < MAX_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE:
                self._recording.append(chunk)

    def take(self) -> bytes | None:
        """The answer's audio since speech started; None if there was no speech."""
        recording, self._recording = self._recording, None
        self._ring.clear()
        return b"".join(recording) if recording else None

    def _on_user_state(self, ev: UserStateChangedEvent) -> None:
        if ev.new_state == SPEAKING and self._recording is None:
            self._recording = list(self._ring)

    def _resample(self, frame: rtc.AudioFrame) -> list[bytes]:
        if frame.sample_rate != self._input_rate:
            self._input_rate = frame.sample_rate
            self._resampler = rtc.AudioResampler(
                frame.sample_rate, SAMPLE_RATE, num_channels=frame.num_channels
            )
        assert self._resampler is not None
        return [bytes(f.data) for f in self._resampler.push(frame)]
