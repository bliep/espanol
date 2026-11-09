"""Drill content on Claude Haiku: item generation and translation judging.

  DrillItemGenerator: level + due words + frequent errors ──► N sentence pairs
                      e.g. {"spanish": "Hoy llevo el paraguas.", "english": "Today I'm taking
                      my umbrella.", "target": "el paraguas"}
  TranslationJudge:   source + expected + learner answer ──► meaning / form scores,
                      target word used?, minimally corrected answer

Both are structured output calls, off the voice path of free conversation.
"""

from typing import Any

import anthropic
from pydantic import BaseModel, Field

from costs import CostMeter
from feedback import VocabItem
from modes import LessonMode
from prompts import Level

DRILL_MODEL = "claude-haiku-5-5"
DRILL_MAX_TOKENS = 4000
DRILL_REQUEST_EXTRAS: dict[str, Any] = {"output_config": {"effort": "low"}}


class DrillItem(BaseModel):
    spanish: str = Field(description="Natural Spain Spanish sentence")
    english: str = Field(description="Natural English translation")
    target: str = Field(description="The practised Spanish word as it appears in the list")
    target_english: str = Field(description="English meaning of the target word")


class _Items(BaseModel):
    items: list[DrillItem]


_GENERATE = """\
You write drill sentences for an English-speaking learner of Spain Spanish (CEFR {level}).
Write exactly {count} items for this drill: {purpose}

Each item: one Spain Spanish sentence at {level} level (grammar and vocabulary the learner
can manage), its natural English translation, and the target word it practises (with its
English meaning). The target must appear in the sentence exactly as written in the item.
- Use these words first, one per item, as given: {words}
- When they run out, pick useful everyday {level} words, a different one per item.
- Weave in these recent error types where natural: {errors}
- {length}
- Vary topics and sentence shapes. No names of real people.
"""

_PURPOSE: dict[LessonMode, str] = {
    LessonMode.TRANSLATE_EN: "the learner hears the English and says it in Spanish.",
    LessonMode.TRANSLATE_ES: "the learner hears the Spanish and says what it means in English.",
    LessonMode.REPEAT: "the learner repeats the Spanish sentence; pronunciation is scored.",
}

_LENGTH: dict[LessonMode, str] = {
    LessonMode.TRANSLATE_EN: "4 to 9 words in Spanish.",
    LessonMode.TRANSLATE_ES: "5 to 10 words in Spanish.",
    LessonMode.REPEAT: "6 to 10 words, with a mix of sounds (r/rr, j, ll, ñ, vowels).",
}


class DrillItemGenerator:
    def __init__(
        self, client: anthropic.AsyncAnthropic | None = None, meter: CostMeter | None = None
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()
        self._meter = meter

    async def generate(
        self,
        mode: LessonMode,
        level: Level,
        count: int,
        words: list[VocabItem],
        error_types: list[str],
    ) -> list[DrillItem]:
        system = _GENERATE.format(
            level=level,
            count=count,
            purpose=_PURPOSE[mode],
            words=", ".join(w.spanish for w in words) or "(none)",
            errors=", ".join(error_types) or "(none)",
            length=_LENGTH[mode],
        )
        response = await self._client.messages.parse(
            model=DRILL_MODEL,
            max_tokens=DRILL_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": f"Write the {count} items."}],
            output_format=_Items,
            **DRILL_REQUEST_EXTRAS,
        )
        if self._meter:
            self._meter.add_claude(DRILL_MODEL, response.usage)
        return response.parsed_output.items[:count]


class TranslationVerdict(BaseModel):
    meaning: int = Field(description="0–100: is the meaning of the source conveyed?")
    form: int = Field(description="0–100: grammar and word forms of the answer")
    target_used: bool = Field(description="Did the answer use the target word (any form)?")
    corrected: str = Field(
        description="The learner's answer with minimal fixes; unchanged if correct"
    )
    note: str = Field(description="One short English line on the main issue; empty if none")


_JUDGE = """\
You grade one translation drill answer from an English-speaking learner of Spain Spanish
(CEFR {level}). The answer is a speech-to-text transcript or typed text: ignore missing
accents, punctuation and capitalisation. Judge meaning first, form second.

meaning: 100 if the source's meaning is fully conveyed (any valid wording, synonyms and
  Latin American forms are fine), lower for missing or wrong parts, 0 if unrelated or empty.
form: grammar, agreement, conjugation and word forms of the answer, in the answer's language.
  Only real errors lower it. Valid alternatives are not errors: other word order, an
  article or possessive instead of the model's (el/mi), pronominal verbs (me llevo).
target_used: whether the target word appears in the answer (any inflection).
corrected: the learner's own answer, minimally fixed; do not replace it with the model answer.
"""


class TranslationJudge:
    def __init__(
        self, client: anthropic.AsyncAnthropic | None = None, meter: CostMeter | None = None
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()
        self._meter = meter

    async def judge(
        self, source: str, expected: str, target: str, answer: str, level: Level
    ) -> TranslationVerdict:
        content = (
            f"Source: {source}\nModel answer: {expected}\nTarget word: {target}\n"
            f"Learner answer: {answer}"
        )
        response = await self._client.messages.parse(
            model=DRILL_MODEL,
            max_tokens=DRILL_MAX_TOKENS,
            system=_JUDGE.format(level=level),
            messages=[{"role": "user", "content": content}],
            output_format=TranslationVerdict,
            **DRILL_REQUEST_EXTRAS,
        )
        if self._meter:
            self._meter.add_claude(DRILL_MODEL, response.usage)
        return response.parsed_output
