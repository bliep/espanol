"""Recorded-turn eval: real learner audio through production STT and the feedback extractor.

Run: cd agent && uv run python evals/recorded.py

  agent/evals/recordings/*.wav + .json (made with RECORD_TURNS=1, reference corrected by hand)
      │  or, if there are none yet, synthetic learner turns (Cartesia)
      ▼
  Deepgram Nova-3, Spanish-only, smart_format off, EU (same settings as pipeline.py)
      ├─ WER vs the reference: do learner mistakes survive transcription?
      └─ FeedbackExtractor on the transcript: are the expected corrections found?

STT time here is a pre-recorded request, not streaming latency. Cost: < $0.01.
"""

import asyncio
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from synth_audio import synth  # noqa: E402

from feedback import FeedbackExtractor  # noqa: E402
from pipeline import STT_LANGUAGE, STT_MODEL  # noqa: E402
from prompts import Level  # noqa: E402
from pronunciation import SAMPLE_RATE  # noqa: E402
from turn_recorder import RECORDINGS  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

DEEPGRAM_EU_LISTEN = "https://api.eu.deepgram.com/v1/listen"
WER_TARGET = 0.15
CORRECTIONS_TARGET = 0.8


@dataclass(frozen=True)
class Turn:
    name: str
    pcm: bytes
    reference: str
    expected_corrections: tuple[str, ...]  # substrings of some `corrected`
    level: Level


# Learner-like turns with typical mistakes, used until real recordings exist.
SYNTHETIC = (
    ("Yo es muy cansado hoy porque trabajé mucho.", ("estoy",)),
    ("Me gusta mucho las montañas de España.", ("gustan",)),
    ("El año pasado vivo en Londres con mi hermana.", ("viví",)),
    ("Mi verdura favorita es la pimiento rojo.", ("el pimiento",)),
    ("Ayer fui al mercado y compré pan y fruta.", ()),
)


def load_recordings() -> list[Turn]:
    turns = []
    for meta_path in sorted(RECORDINGS.glob("*.json")):
        meta = json.loads(meta_path.read_text())
        with wave.open(str(meta_path.with_suffix(".wav")), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        turns.append(
            Turn(meta_path.stem, pcm, meta["reference"], tuple(meta["expected_corrections"]),
                 Level(meta["level"]))
        )  # fmt: skip
    return turns


async def synthetic_turns(http: aiohttp.ClientSession) -> list[Turn]:
    pcms = await asyncio.gather(*(synth(http, text) for text, _ in SYNTHETIC))
    return [
        Turn(f"synthetic-{i}", pcm, text, expected, Level.A2)
        for i, ((text, expected), pcm) in enumerate(zip(SYNTHETIC, pcms, strict=True))
    ]


async def transcribe(http: aiohttp.ClientSession, pcm: bytes) -> tuple[str, float]:
    params = {
        "model": STT_MODEL,
        "language": STT_LANGUAGE,
        "smart_format": "false",  # keep learner mistakes (pipeline.py)
        "punctuate": "true",
        "encoding": "linear16",
        "sample_rate": str(SAMPLE_RATE),
        "channels": "1",
    }
    headers = {"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}"}
    start = time.perf_counter()
    async with http.post(DEEPGRAM_EU_LISTEN, params=params, headers=headers, data=pcm) as r:
        r.raise_for_status()
        body = await r.json()
    elapsed = (time.perf_counter() - start) * 1000
    return body["results"]["channels"][0]["alternatives"][0]["transcript"], elapsed


def words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(ch for ch in plain if unicodedata.category(ch) != "Mn")
    return re.findall(r"[a-z0-9']+", plain)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word-level edit distance / reference length."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / max(len(ref), 1)


async def main() -> int:
    extractor = FeedbackExtractor()
    async with aiohttp.ClientSession() as http:
        turns = load_recordings()
        source = f"{len(turns)} recordings"
        if not turns:
            turns = await synthetic_turns(http)
            source = f"no recordings yet: {len(turns)} synthetic turns"
        print(f"{source}\n")

        rates, stt_ms, found, expected = [], [], 0, 0
        for turn in turns:
            heard, ms = await transcribe(http, turn.pcm)
            rate = wer(turn.reference, heard)
            rates.append(rate)
            stt_ms.append(ms)

            fb = await extractor.extract(heard, "", turn.level)
            corrected = " ".join(c.corrected.lower() for c in fb.corrections)
            hits = [e for e in turn.expected_corrections if e.lower() in corrected]
            found += len(hits)
            expected += len(turn.expected_corrections)

            missing = set(turn.expected_corrections) - set(hits)
            flag = "✓" if rate <= WER_TARGET and not missing else "✗"
            print(f"{flag} {turn.name}: WER {rate:.0%} | {heard!r}")
            if rate > 0:
                print(f"     reference {turn.reference!r}")
            if missing:
                print(f"     missing corrections {sorted(missing)} (got {corrected!r})")

    mean_wer = statistics.mean(rates)
    recall = found / expected if expected else 1.0
    print(
        f"\nWER mean {mean_wer:.0%} (target ≤ {WER_TARGET:.0%}) | corrections found "
        f"{found}/{expected} | STT request median {statistics.median(stt_ms):.0f} ms"
    )
    return 0 if mean_wer <= WER_TARGET and recall >= CORRECTIONS_TARGET else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
