"""Pronunciation scorer eval: does Azure (via pronunciation.py) separate good from bad?

Run: cd agent && uv run python evals/pronunciation.py

Audio is synthesised with Cartesia (the tutor's voice), so no microphone is needed:
  reference sentence read correctly     → high score, no word errors
  vowels changed in two words           → those words flagged, lower score
  three words left out                  → those words omitted, low completeness
  correct, with a 2 s pause mid-sentence → still complete (continuous recognition)

Note: Spanish b/v and ll/y sound the same, so "lebo" for "llevo" is NOT an error;
garbles must change vowels or drop words. Cost: well under $0.01.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from synth_audio import synth  # noqa: E402

from pronunciation import SAMPLE_RATE, PronunciationAssessor, WordError  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

BYTES_PER_SAMPLE = 2

REFERENCE = "Hoy llevo el paraguas porque llueve mucho."
PAUSE_S = 2.0

GOOD_SCORE = 90
BAD_SCORE = 85
LOW_COMPLETENESS = 70


@dataclass(frozen=True)
class Case:
    name: str
    spoken: tuple[str, ...]  # parts joined with PAUSE_S of silence
    min_score: float = 0
    max_score: float = 100
    min_completeness: float = 0
    max_completeness: float = 100
    mispronounced: tuple[str, ...] = ()
    omitted: tuple[str, ...] = ()
    clean: bool = False  # no word errors allowed


CASES = (
    Case("correct", (REFERENCE,), min_score=GOOD_SCORE, min_completeness=100, clean=True),
    Case(
        "vowels changed",
        ("Hay lleva al pareguos parqui lluvia macha.",),
        max_score=BAD_SCORE,
        mispronounced=("llueve", "mucho"),
    ),
    Case(
        "words left out",
        ("Hoy llevo porque mucho.",),
        max_completeness=LOW_COMPLETENESS,
        omitted=("el", "paraguas", "llueve"),
    ),
    Case(
        "pause mid-sentence",
        ("Hoy llevo el paraguas", "porque llueve mucho."),
        min_score=GOOD_SCORE,
        min_completeness=100,
    ),
)


@dataclass
class Outcome:
    case: Case
    problems: list[str] = field(default_factory=list)


async def run(http: aiohttp.ClientSession, assessor: PronunciationAssessor, case: Case) -> Outcome:
    silence = bytes(int(PAUSE_S * SAMPLE_RATE) * BYTES_PER_SAMPLE)
    parts = [await synth(http, text) for text in case.spoken]
    result = await assessor.assess(silence.join(parts), REFERENCE)

    words = {w.word: w for w in result.words}
    errors = [f"{w.word}:{w.error}" for w in result.words if w.error != WordError.NONE]
    print(
        f"{case.name}: score {result.score:.0f}, completeness {result.completeness:.0f}, "
        f"fluency {result.fluency:.0f} | heard {result.heard!r} | errors {errors}"
    )

    outcome = Outcome(case)
    if not case.min_score <= result.score <= case.max_score:
        outcome.problems.append(f"score {result.score} not in {case.min_score}–{case.max_score}")
    if not case.min_completeness <= result.completeness <= case.max_completeness:
        outcome.problems.append(f"completeness {result.completeness}")
    for word in case.mispronounced:
        if words[word].error != WordError.MISPRONOUNCED:
            outcome.problems.append(f"{word} not flagged mispronounced")
    for word in case.omitted:
        if words[word].error != WordError.OMITTED:
            outcome.problems.append(f"{word} not flagged omitted")
    if case.clean and errors:
        outcome.problems.append(f"unexpected errors {errors}")
    return outcome


async def main() -> int:
    assessor = PronunciationAssessor()
    async with aiohttp.ClientSession() as http:
        outcomes = [await run(http, assessor, case) for case in CASES]

    failed = [o for o in outcomes if o.problems]
    for o in failed:
        print(f"  ✗ {o.case.name}: {'; '.join(o.problems)}")
    print(f"\n{len(outcomes) - len(failed)}/{len(outcomes)} cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
