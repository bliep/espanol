"""Feedback extractor eval: right corrections, no false alarms, vocab gaps, language.

Run: cd agent && uv run python evals/feedback.py [--runs 3]

Checks are deterministic (substring / field checks on the structured output), so
results only vary with the model's sampling. Cost: <$0.01 per run.
"""

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from feedback import Feedback, FeedbackExtractor, UserLanguage  # noqa: E402
from prompts import DEFAULT_LEVEL  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

PASS_RATE_TARGET = 0.9
LATENCY_TARGET_MS = 2000  # Phase 2: feedback within 2 s of the turn


@dataclass(frozen=True)
class Case:
    learner: str
    language: UserLanguage
    # Each inner tuple: one expected correction; any of its strings must appear in
    # some `corrected`. Empty tuple = the turn is correct, no corrections allowed.
    corrections: tuple[tuple[str, ...], ...] = ()
    # Each inner tuple: one expected vocab item; any string must appear in `spanish`.
    vocab: tuple[tuple[str, ...], ...] = ()
    tutor_prev: str = ""


CASES: tuple[Case, ...] = (
    Case("Ayer fui al supermercado y compré pan.", UserLanguage.SPANISH),
    Case("Me llamo Anuj y vivo en Ámsterdam.", UserLanguage.SPANISH),
    Case("El verano pasado fui a los Pirineos con mis amigos.", UserLanguage.SPANISH),
    Case("Yo es muy cansado hoy.", UserLanguage.SPANISH, corrections=(("estoy",),)),
    Case("Me gusta mucho las montañas.", UserLanguage.SPANISH, corrections=(("gustan",),)),
    Case("Mi verdura favorita es la pimiento.", UserLanguage.SPANISH,
         corrections=(("el pimiento",),)),
    Case("Ayer yo comí en un restaurante y la comida es muy buena.", UserLanguage.SPANISH,
         corrections=(("era", "estaba", "fue"),)),
    Case("Mi hermana is a teacher en Utrecht.", UserLanguage.MIXED,
         vocab=(("profesor", "maestr"),)),
    Case("Necesito comprar arroz y chicken.", UserLanguage.MIXED, vocab=(("pollo",),)),
    Case("Quiero... how do you say... travel a España.", UserLanguage.MIXED, vocab=(("viajar",),)),
    Case("Yesterday I went to the beach with my friends.", UserLanguage.ENGLISH,
         vocab=(("playa",),)),
    Case("How do you say 'bike' in Spanish?", UserLanguage.ENGLISH, vocab=(("bici",),)),
    # From the live long chat (Phase 2), with the tutor line that preceded them.
    Case("No sé cocinar bien, pero me gusta.", UserLanguage.SPANISH,
         tutor_prev="¡Qué rico, vas a cocinar paella el fin de semana! ¿Es la primera vez?"),
    Case("Caminamos mucho y la comida era increíble.", UserLanguage.SPANISH,
         tutor_prev='Ah, quieres decir: "Caminamos mucho y la comida estaba buenísima". '
                    "¿Puedes repetirlo en español?"),
    Case("Vale, vale. Hablo español.", UserLanguage.SPANISH,
         tutor_prev="Claro, dime en inglés qué necesitas, pero yo sigo en español."),
    Case("They are very lazy.", UserLanguage.ENGLISH, vocab=(("perezos",),),
         tutor_prev="¡Qué bonito, tienes dos gatos! ¿Cómo son?"),
    Case("I work with computers all day.", UserLanguage.ENGLISH, vocab=(("ordenador",),),
         tutor_prev="¡Qué interesante! ¿Y qué tipo de ingeniero eres?"),
    Case("Please, can we speak English for a moment?", UserLanguage.ENGLISH,
         tutor_prev="¡Qué rico! ¿Vas a cocinar para tu familia o para tus amigos?"),
)  # fmt: skip

# Basic A1 words are not "new" for an A2 learner; listing them is noise.
TRIVIAL_VOCAB = {"muy", "mucho", "bien", "son", "es", "y", "con", "el", "la"}

MAX_VOCAB_WORDS = 4  # vocab items are words or short phrases, not whole sentences


@dataclass
class Result:
    case: Case
    feedback: Feedback
    latency_ms: float
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def check(case: Case, fb: Feedback) -> list[str]:
    problems = []
    if fb.user_language != case.language:
        problems.append(f"language {fb.user_language} != {case.language}")

    corrected = [c.corrected.lower() for c in fb.corrections]
    # Mixed/English turns with correct Spanish get vocab, not corrections.
    if not case.corrections and corrected:
        problems.append(f"false correction(s): {corrected}")
    noops = [c.original for c in fb.corrections if c.original.strip() == c.corrected.strip()]
    if noops:
        problems.append(f"no-op correction(s): {noops}")
    for expected in case.corrections:
        if not any(e in c for e in expected for c in corrected):
            problems.append(f"missing correction {expected}")

    spanish = [v.spanish.lower() for v in fb.new_vocab]
    too_long = [s for s in spanish if len(s.split()) > MAX_VOCAB_WORDS]
    if too_long:
        problems.append(f"sentence-length vocab: {too_long}")
    trivial = [s for s in spanish if s in TRIVIAL_VOCAB]
    if trivial:
        problems.append(f"trivial vocab: {trivial}")
    for expected in case.vocab:
        if not any(e in s for e in expected for s in spanish):
            problems.append(f"missing vocab {expected} (got {spanish})")

    return problems


async def evaluate(extractor: FeedbackExtractor, case: Case) -> Result:
    start = time.perf_counter()
    fb = await extractor.extract(case.learner, case.tutor_prev, DEFAULT_LEVEL)
    latency_ms = (time.perf_counter() - start) * 1000
    return Result(case, fb, latency_ms, check(case, fb))


def report(runs: list[list[Result]]) -> tuple[float, float]:
    for i, case in enumerate(CASES):
        results = [run[i] for run in runs]
        passed = sum(r.passed for r in results)
        print(f"{passed}/{len(results)} {case.learner}")
        for r in results:
            if r.passed:
                continue
            print(f"     ✗ {'; '.join(r.problems)}")
            print(f"       {r.feedback.model_dump_json()}")

    flat = [r for run in runs for r in run]
    rate = sum(r.passed for r in flat) / len(flat)
    latencies = sorted(r.latency_ms for r in flat)
    p90 = latencies[int(0.9 * (len(latencies) - 1))]
    print(
        f"\npass rate {rate:.0%} (target {PASS_RATE_TARGET:.0%}) | latency median "
        f"{statistics.median(latencies):.0f} ms, p90 {p90:.0f} ms (target {LATENCY_TARGET_MS})"
    )
    return rate, p90


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="samples per case")
    args = parser.parse_args()

    extractor = FeedbackExtractor()
    runs = [
        list(await asyncio.gather(*(evaluate(extractor, c) for c in CASES)))
        for _ in range(args.runs)
    ]
    rate, p90 = report(runs)
    return 0 if rate >= PASS_RATE_TARGET and p90 <= LATENCY_TARGET_MS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
