"""Builds the cascaded voice pipeline: VAD → STT → LLM → TTS.

Provider choices and their settings live here, so main.py only deals
with sessions and agents.
"""

from livekit.agents import AgentSession, TurnHandlingOptions, inference, vad
from livekit.plugins import anthropic, cartesia, deepgram, silero

# Deepgram EU region (user is in NL). Plugin converts https → wss.
DEEPGRAM_EU_URL = "https://api.eu.deepgram.com/v1/listen"
STT_MODEL = "nova-3"
STT_LANGUAGE = "multi"  # EN/ES code-switching in one stream

LLM_MODEL = "claude-sonnet-5-5"

TTS_MODEL = "sonic-3.5"
TTS_LANGUAGE = "es"
VOICE_BLANCA_ES = "538a8872-3799-4df5-b373-b78493b766c6"  # Castilian female

# Learners pause to search for words; allow long silences before ending a turn.
MAX_ENDPOINTING_DELAY_S = 5.0


def load_vad() -> vad.VAD:
    """Silero VAD; loaded once per worker process (slow)."""
    return silero.VAD.load()


def build_session(vad_model: vad.VAD) -> AgentSession:
    stt = deepgram.STT(
        model=STT_MODEL,
        language=STT_LANGUAGE,
        smart_format=False,  # keep learner mistakes visible
        base_url=DEEPGRAM_EU_URL,
    )
    llm = anthropic.LLM(model=LLM_MODEL)
    tts = cartesia.TTS(model=TTS_MODEL, language=TTS_LANGUAGE, voice=VOICE_BLANCA_ES)

    turns = TurnHandlingOptions(
        turn_detection=inference.TurnDetector(),
        endpointing={"max_delay": MAX_ENDPOINTING_DELAY_S},
    )

    return AgentSession(stt=stt, llm=llm, tts=tts, vad=vad_model, turn_handling=turns)
