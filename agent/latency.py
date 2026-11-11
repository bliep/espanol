"""Per-turn voice-to-voice latency logging.

LiveKit emits one metric per stage, tagged with the reply's speech_id.
We collect them per turn and log one line once all three arrive:

  user stops ──► STT final ──► turn committed ──► LLM 1st token ──► TTS 1st audio
             └── stt_final ─┘
             └────────── eou ──────────────┘└─── llm_ttft ───┘└── tts_ttfb ──┘
  total = eou + llm_ttft + tts_ttfb  (an upper bound: preemptive generation starts the
          LLM before the turn is committed, so stages overlap)

The true figure is LiveKit's per-reply e2e_latency (user stops ──► agent starts
speaking), logged separately as "e2e" when the reply is added to the chat history.
"""

import logging
from dataclasses import dataclass

from livekit.agents import AgentSession, ConversationItemAddedEvent, MetricsCollectedEvent
from livekit.agents.metrics import EOUMetrics, LLMMetrics, TTSMetrics

TARGET_MS = 800
MAX_TRACKED_TURNS = 50  # oldest dropped first; bounds memory

log = logging.getLogger("tutor.latency")


@dataclass
class _Turn:
    eou: EOUMetrics | None = None
    llm: LLMMetrics | None = None
    tts: TTSMetrics | None = None
    logged: bool = False

    def complete(self) -> bool:
        return self.eou is not None and self.llm is not None and self.tts is not None


class LatencyLogger:
    def __init__(self) -> None:
        self._turns: dict[str, _Turn] = {}

    def attach(self, session: AgentSession) -> None:
        session.on("metrics_collected", self._on_metrics)
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        """LiveKit's measured voice-to-voice latency of a reply to a spoken turn."""
        # Handoff items (mode switches) have no role or metrics.
        if getattr(ev.item, "role", None) != "assistant":
            return
        e2e = (ev.item.metrics or {}).get("e2e_latency")
        if e2e is None:
            return

        e2e_ms = e2e * 1000
        verdict = "OK" if e2e_ms <= TARGET_MS else "SLOW"
        log.info("e2e %s | %4.0f ms %s (target %d)", ev.item.id, e2e_ms, verdict, TARGET_MS)

    def _on_metrics(self, ev: MetricsCollectedEvent) -> None:
        m = ev.metrics
        speech_id = getattr(m, "speech_id", None)
        if not speech_id:
            return

        turn = self._turns.setdefault(speech_id, _Turn())
        if turn.logged:
            return

        # Keep only the first metric per stage (TTS emits one per sentence).
        if isinstance(m, EOUMetrics) and turn.eou is None:
            turn.eou = m
        elif isinstance(m, LLMMetrics) and turn.llm is None:
            turn.llm = m
        elif isinstance(m, TTSMetrics) and turn.tts is None:
            turn.tts = m

        if turn.complete():
            turn.logged = True  # kept so later per-sentence TTS metrics are ignored
            self._log(speech_id, turn)
        elif turn.eou is None and turn.llm is not None and turn.tts is not None:
            # Typed turn or greeting: no speech, so no EOU metric ever arrives.
            turn.logged = True
            self._log_text_turn(speech_id, turn)

        while len(self._turns) > MAX_TRACKED_TURNS:
            self._turns.pop(next(iter(self._turns)))

    def _log(self, speech_id: str, turn: _Turn) -> None:
        stt_ms = turn.eou.transcription_delay * 1000
        eou_ms = turn.eou.end_of_utterance_delay * 1000
        llm_ms = turn.llm.ttft * 1000
        tts_ms = turn.tts.ttfb * 1000
        total_ms = eou_ms + llm_ms + tts_ms
        verdict = "OK" if total_ms <= TARGET_MS else "SLOW"

        log.info(
            "turn %s | stt_final %4.0f | eou %4.0f | llm_ttft %4.0f | tts_ttfb %4.0f "
            "| total %4.0f ms %s (target %d)",
            speech_id,
            stt_ms,
            eou_ms,
            llm_ms,
            tts_ms,
            total_ms,
            verdict,
            TARGET_MS,
        )

    def _log_text_turn(self, speech_id: str, turn: _Turn) -> None:
        llm_ms = turn.llm.ttft * 1000
        tts_ms = turn.tts.ttfb * 1000
        log.info(
            "text turn %s | llm_ttft %4.0f | tts_ttfb %4.0f | total %4.0f ms",
            speech_id,
            llm_ms,
            tts_ms,
            llm_ms + tts_ms,
        )
