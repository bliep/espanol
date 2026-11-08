"""Recast eval: does the tutor follow the language policy on fixed learner turns?

Run: cd agent && uv run python evals/recast.py [--runs 3]

  learner turn ──► tutor (production LLM + prompt) ──► reply
                                                         │
                       deterministic checks ◄────────────┤  sentences, markdown, ends with "?"
                       judge (Claude Haiku) ◄────────────┘  Spanish only? policy followed?

Exit code 1 if the pass rate is below PASS_RATE_TARGET. Cost: ~$0.02 per run.
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from livekit.agents import llm as lk  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import pipeline  # noqa: E402
from prompts import DEFAULT_LEVEL, build_instructions  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

JUDGE_MODEL = "claude-haiku-5-5"
MAX_SENTENCES = 3  # CLAUDE.md: spoken replies 1–3 sentences
PASS_RATE_TARGET = 0.9  # replies are sampled; demand most, not all


class Kind(StrEnum):
    SPANISH = "spanish"
    ENGLISH = "english"
    MIXED = "mixed"
    HELP = "help"  # learner asks how to say something
    INSIST = "insist"  # learner asks the tutor to switch to English


# What the judge checks per kind of learner turn.
POLICY: dict[Kind, str] = {
    Kind.SPANISH: (
        "Reacts to the content. If the learner made a mistake, at most one implicit recast "
        "(the corrected form used naturally, e.g. 'Yo es cansado' → 'Ah, estás cansado'; "
        "second person or the tutor's own sentence is fine), without words like "
        "'error'/'incorrecto' and without grammar explanations. If no mistake, no correction."
    ),
    Kind.ENGLISH: (
        "Recasts the learner's English idea as a Spanish sentence and invites them to "
        "repeat or say it in Spanish."
    ),
    Kind.MIXED: (
        "Supplies the missing Spanish word(s) in context, e.g. by repeating the learner's "
        "sentence fully in Spanish, then continues the conversation."
    ),
    Kind.HELP: "Gives the Spanish word with a short example, then continues in Spanish.",
    Kind.INSIST: "Stays in Spanish (at most one short English phrase); does not switch to English.",
}


@dataclass(frozen=True)
class Case:
    kind: Kind
    learner: str
    context: tuple[str, ...] = ()  # earlier turns, alternating tutor/learner, tutor first


CASES: tuple[Case, ...] = (
    Case(Kind.SPANISH, "Hola, me llamo Anuj y vivo en Ámsterdam."),
    Case(Kind.SPANISH, "Ayer fui al supermercado y compré pan."),
    Case(Kind.SPANISH, "Yo es muy cansado hoy."),
    Case(Kind.SPANISH, "Me gusta mucho las películas de terror."),
    Case(Kind.ENGLISH, "I don't know what to say."),
    Case(Kind.ENGLISH, "Yesterday I went to the beach with my friends."),
    Case(Kind.ENGLISH, "I work as a software engineer."),
    Case(Kind.MIXED, "Ayer fui al... store para comprar leche."),
    Case(Kind.MIXED, "Mi hermana is a doctor en Utrecht."),
    Case(Kind.MIXED, "Quiero... how do you say... travel a España."),
    Case(Kind.HELP, "How do you say 'bike' in Spanish?"),
    Case(
        Kind.INSIST,
        "Please, just speak English with me.",
        context=("¡Hola! ¿Qué hiciste el fin de semana?", "Eh... no sé. Es difícil."),
    ),
)


class Verdict(BaseModel):
    spanish_only: bool
    policy_followed: bool
    reason: str


@dataclass
class Result:
    case: Case
    reply: str
    sentences: int
    has_markup: bool
    ends_with_question: bool
    verdict: Verdict

    @property
    def passed(self) -> bool:
        return (
            self.sentences <= MAX_SENTENCES
            and not self.has_markup
            and self.verdict.spanish_only
            and self.verdict.policy_followed
        )


# "¡Hola! Me alegro. ¿Qué tal?" → 3 sentences. Ellipses ("al... store") don't split.
_SENTENCE_END = re.compile(r"(?<![.])[.!?](?![.])[\"”»]?(?=\s|$)")
_MARKUP = re.compile(r"[*_#`\[\]]|[\U0001F300-\U0001FAFF☀-➿]")


def count_sentences(text: str) -> int:
    return max(1, len(_SENTENCE_END.findall(text.strip())))


async def tutor_reply(tutor: lk.LLM, case: Case) -> str:
    ctx = lk.ChatContext()
    ctx.add_message(role="system", content=build_instructions(DEFAULT_LEVEL))
    for i, text in enumerate(case.context):
        ctx.add_message(role="assistant" if i % 2 == 0 else "user", content=text)
    ctx.add_message(role="user", content=case.learner)

    reply = ""
    async with tutor.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                reply += chunk.delta.content
    return reply.strip()


async def judge(client: anthropic.AsyncAnthropic, case: Case, reply: str) -> Verdict:
    prompt = (
        "You grade a Spanish tutor's spoken reply to an English-speaking A2 learner.\n\n"
        f"Learner said: {case.learner!r}\n"
        f"Tutor replied: {reply!r}\n\n"
        "spanish_only: true if the reply is in Spanish. Quoting the learner's own English "
        "word, or one short English lifeline phrase, is allowed. Spanish words about "
        "languages ('inglés', 'español') are Spanish.\n"
        f"policy_followed: true if the reply does this: {POLICY[case.kind]}\n"
        "reason: one short sentence."
    )
    response = await client.messages.parse(
        model=JUDGE_MODEL,
        max_tokens=2000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Verdict,
    )
    return response.parsed_output


async def evaluate(tutor: lk.LLM, client: anthropic.AsyncAnthropic, case: Case) -> Result:
    reply = await tutor_reply(tutor, case)
    verdict = await judge(client, case, reply)
    return Result(
        case=case,
        reply=reply,
        sentences=count_sentences(reply),
        has_markup=bool(_MARKUP.search(reply)),
        ends_with_question=reply.rstrip('"”» ').endswith("?"),
        verdict=verdict,
    )


def report(runs: list[list[Result]]) -> float:
    """Per-case pass rate over all runs; failing replies printed. Returns overall rate."""
    for i, case in enumerate(CASES):
        results = [run[i] for run in runs]
        passed = sum(r.passed for r in results)
        print(f"{passed}/{len(results)} [{case.kind}] {case.learner}")

        # Show one reply when all pass, every failing reply otherwise.
        shown = [r for r in results if not r.passed] or results[:1]
        for r in shown:
            print(f"     → {r.reply}")
            if r.passed:
                continue
            print(
                f"       sentences={r.sentences} markup={r.has_markup} "
                f"spanish={r.verdict.spanish_only} policy={r.verdict.policy_followed} "
                f"| {r.verdict.reason}"
            )

    flat = [r for run in runs for r in run]
    rate = sum(r.passed for r in flat) / len(flat)
    questions = sum(r.ends_with_question for r in flat) / len(flat)
    print(f"\npass rate {rate:.0%} (target {PASS_RATE_TARGET:.0%}) | ends with ? {questions:.0%}")
    return rate


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="samples per case (LLM variance)")
    args = parser.parse_args()

    tutor = pipeline.build_llm()
    client = anthropic.AsyncAnthropic()

    runs = [
        list(await asyncio.gather(*(evaluate(tutor, client, c) for c in CASES)))
        for _ in range(args.runs)
    ]
    rate = report(runs)
    return 0 if rate >= PASS_RATE_TARGET else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
