"""Test audio for evals: Spanish sentences spoken by the tutor's Cartesia voice.

Returns 16 kHz, 16-bit mono PCM (pronunciation.SAMPLE_RATE), as a learner's mic would
deliver after resampling.
"""

import os
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pronunciation import SAMPLE_RATE  # noqa: E402

CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2025-04-16"
TTS_MODEL = "sonic-3.5"
VOICE_BLANCA_ES = "538a8872-3799-4df5-b373-b78493b766c6"  # agent/pipeline.py


async def synth(http: aiohttp.ClientSession, text: str) -> bytes:
    body = {
        "model_id": TTS_MODEL,
        "transcript": text,
        "language": "es",
        "voice": {"mode": "id", "id": VOICE_BLANCA_ES},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
    }
    headers = {"X-API-Key": os.environ["CARTESIA_API_KEY"], "Cartesia-Version": CARTESIA_VERSION}
    async with http.post(CARTESIA_URL, json=body, headers=headers) as r:
        r.raise_for_status()
        return await r.read()
