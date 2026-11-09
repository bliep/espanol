"""Translation drill eval: judge verdicts on fixed answers, and generated items.

Run: cd agent && uv run python evals/translation.py [--runs 3]

Judge cases have bounds on meaning / form, target use and the correction.
Generator check: right count, given words used as targets, target inside each sentence.
Cost: ~$0.01 per run.
"""

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from drill_content import DrillItemGenerator, TranslationJudge, TranslationVerdict  # noqa: E402
from feedback import VocabItem  # noqa: E402
from learner_model import term_of, used_terms  # noqa: E402
from modes import LessonMode  # noqa: E402
from prompts import DEFAULT_LEVEL  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

PASS_RATE_TARGET = 0.9
DRILL_SIZE = 8
GOOD = 80  # meaning/form at or above: correct
POOR = 50  # meaning/form at or below: clearly wrong


@dataclass(frozen=True)
class Case:
    source: str
    expected: str
    answer: str
    target: str = ""  # EN→ES only
    min_meaning: int = 0
    max_meaning: int = 100
    min_form: int = 0
    max_form: int = 100
    target_used: bool | None = None
    corrected_has: str = ""


CASES = (
    Case("I'm taking my umbrella today.", "Hoy llevo mi paraguas.", "Hoy llevo mi paraguas",
         target="paraguas", min_meaning=GOOD, min_form=GOOD, target_used=True),
    Case("I'm taking my umbrella today.", "Hoy llevo mi paraguas.", "Hoy me llevo el paraguas",
         target="paraguas", min_meaning=GOOD, min_form=GOOD, target_used=True),
    Case("Yesterday I went to the market.", "Ayer fui al mercado.", "Ayer yo voy al mercado",
         target="mercado", min_meaning=POOR, max_form=GOOD - 1, corrected_has="fui"),
    Case("I bought a chicken.", "Compré un pollo.", "Compré un pescado",
         target="pollo", max_meaning=POOR, target_used=False),
    Case("Tomorrow I'm going to the station.", "Mañana voy a la estación.",
         "Mañana voy a la estacion", target="estación", min_meaning=GOOD, min_form=GOOD),
    Case("My sister is tired.", "Mi hermana está cansada.", "Mi hermana es cansado",
         target="cansada", min_meaning=POOR, max_form=GOOD - 1, corrected_has="está"),
    Case("Where is the bathroom?", "¿Dónde está el baño?", "I don't know",
         target="baño", max_meaning=20),
    Case("Mi hermana trabaja en un hospital.", "My sister works in a hospital.",
         "My sister works at a hospital", min_meaning=GOOD),
    Case("Mi hermana trabaja en un hospital.", "My sister works in a hospital.",
         "My brother lives in a house", max_meaning=POOR),
)  # fmt: skip


def check(case: Case, v: TranslationVerdict) -> list[str]:
    problems = []
    if not case.min_meaning <= v.meaning <= case.max_meaning:
        problems.append(f"meaning {v.meaning} not in {case.min_meaning}–{case.max_meaning}")
    if not case.min_form <= v.form <= case.max_form:
        problems.append(f"form {v.form} not in {case.min_form}–{case.max_form}")
    if case.target_used is not None and v.target_used != case.target_used:
        problems.append(f"target_used {v.target_used}")
    if case.corrected_has and case.corrected_has not in v.corrected.lower():
        problems.append(f"corrected {v.corrected!r} lacks {case.corrected_has!r}")
    return problems


async def judge_cases(judge: TranslationJudge) -> tuple[int, int]:
    start = time.perf_counter()
    verdicts = await asyncio.gather(
        *(judge.judge(c.source, c.expected, c.target, c.answer, DEFAULT_LEVEL) for c in CASES)
    )
    elapsed = (time.perf_counter() - start) * 1000

    passed = 0
    for case, verdict in zip(CASES, verdicts, strict=True):
        problems = check(case, verdict)
        passed += not problems
        mark = "✓" if not problems else "✗"
        print(f"{mark} {case.answer!r}: meaning {verdict.meaning}, form {verdict.form}, "
              f"target {verdict.target_used}, corrected {verdict.corrected!r}")  # fmt: skip
        for p in problems:
            print(f"     {p}")
    print(f"  (all {len(CASES)} judged in parallel: {elapsed:.0f} ms)")
    return passed, len(CASES)


async def generator_check(generator: DrillItemGenerator) -> tuple[int, int]:
    words = [VocabItem(spanish="el paraguas", english="umbrella"),
             VocabItem(spanish="perezoso", english="lazy"),
             VocabItem(spanish="la playa", english="beach")]  # fmt: skip
    start = time.perf_counter()
    items = await generator.generate(
        LessonMode.TRANSLATE_EN, DEFAULT_LEVEL, DRILL_SIZE, words, ["ser_estar"]
    )
    elapsed = (time.perf_counter() - start) * 1000

    problems = []
    if len(items) != DRILL_SIZE:
        problems.append(f"{len(items)} items")
    targets = {term_of(i.target) for i in items}
    missing = {term_of(w.spanish) for w in words} - targets
    if missing:
        problems.append(f"given words not used: {missing}")
    for i in items:
        if not used_terms(i.spanish, {term_of(i.target)}):
            problems.append(f"target {i.target!r} not in {i.spanish!r}")

    print(
        f"\ngenerator ({elapsed:.0f} ms): " + "; ".join(f"{i.spanish} [{i.target}]" for i in items)
    )
    for p in problems:
        print(f"     ✗ {p}")
    return (0 if problems else 1), 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    judge, generator = TranslationJudge(), DrillItemGenerator()
    passed = total = 0
    for _ in range(args.runs):
        for p, t in (await judge_cases(judge), await generator_check(generator)):
            passed += p
            total += t

    rate = passed / total
    print(f"\npass rate {rate:.0%} (target {PASS_RATE_TARGET:.0%})")
    return 0 if rate >= PASS_RATE_TARGET else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
