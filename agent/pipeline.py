"""Builds the cascaded voice pipeline: VAD → STT → LLM → TTS.

Provider choices and their settings live here, so main.py only deals
with sessions and agents.
"""

from typing import Any

from livekit.agents import AgentSession, TurnHandlingOptions, inference, vad
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins import anthropic, cartesia, deepgram, silero

from prompts import Level

# Deepgram EU region (user is in NL). Plugin converts https → wss.
DEEPGRAM_EU_URL = "https://api.eu.deepgram.com/v1/listen"
STT_MODEL = "nova-3"
STT_LANGUAGE = "multi"  # EN/ES code-switching in one stream

LLM_MODEL = "claude-sonnet-5-5"

# Sonnet 5.5 defaults to adaptive thinking at effort "high": ~2.4 s to first token.
# Measured 2026-11-08 over 5-turn chats, median TTFT:
#   low effort, thinking on   ~1.1 s (some turns still think 1–2 s)
#   low effort, thinking off  ~0.85 s, steadier, same replies
# "between_tools" is Sonnet 5.5's thinking-off mode ("disabled" is rejected).
LLM_REQUEST_EXTRAS: dict[str, Any] = {
    "thinking": {"type": "between_tools"},
    "output_config": {"effort": "low"},
}

TTS_MODEL = "sonic-3.5"
TTS_LANGUAGE = "es"
VOICE_BLANCA_ES = "538a8872-3799-4df5-b373-b78493b766c6"  # Castilian female

# Slower speech for beginners. Cartesia sonic-3.x accepts 0.6–1.5.
TTS_SPEED: dict[Level, float] = {
    Level.A1: 0.85,
    Level.A2: 0.9,
    Level.B1: 0.95,
    Level.B2: 1.0,
    Level.C1: 1.0,
}

SLOW_REPLAY_SPEED = 0.7  # "slow replay" button; Cartesia sonic-3.x accepts 0.6–1.5

# Learners pause to search for words; allow long silences before ending a turn.
MAX_ENDPOINTING_DELAY_S = 5.0


class _TutorLLM(anthropic.LLM):
    """Anthropic LLM that adds LLM_REQUEST_EXTRAS to every call.

    The plugin only accepts extra request fields per chat() call, not at construction.
    """

    def chat(self, *, extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN, **kwargs: Any):
        extra = {**LLM_REQUEST_EXTRAS, **(extra_kwargs or {})}
        return super().chat(extra_kwargs=extra, **kwargs)


def build_llm() -> anthropic.LLM:
    """Tutor LLM as used in production; also used by evals/."""
    # Caching keeps first-token time flat as the history grows (min prefix 512 tokens).
    return _TutorLLM(model=LLM_MODEL, caching="ephemeral")


def load_vad() -> vad.VAD:
    """Silero VAD; loaded once per worker process (slow)."""
    return silero.VAD.load()


def build_slow_tts() -> cartesia.TTS:
    """Same voice at replay speed, separate from the session's TTS (its speed is untouched)."""
    return cartesia.TTS(
        model=TTS_MODEL, language=TTS_LANGUAGE, voice=VOICE_BLANCA_ES, speed=SLOW_REPLAY_SPEED
    )


def build_session(vad_model: vad.VAD, level: Level, keyterms: list[str]) -> AgentSession:
    """keyterms: the day's target words, so Deepgram recognises exactly what is practised."""
    stt = deepgram.STT(
        model=STT_MODEL,
        language=STT_LANGUAGE,
        smart_format=False,  # keep learner mistakes visible
        base_url=DEEPGRAM_EU_URL,
        keyterm=keyterms or NOT_GIVEN,
    )
    tts = cartesia.TTS(
        model=TTS_MODEL,
        language=TTS_LANGUAGE,
        voice=VOICE_BLANCA_ES,
        speed=TTS_SPEED[level],
    )

    turns = TurnHandlingOptions(
        turn_detection=inference.TurnDetector(),
        endpointing={"max_delay": MAX_ENDPOINTING_DELAY_S},
    )

    return AgentSession(stt=stt, llm=build_llm(), tts=tts, vad=vad_model, turn_handling=turns)
