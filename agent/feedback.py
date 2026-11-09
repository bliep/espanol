"""Explicit feedback on each learner turn, for the side panel (never spoken).

The tutor only recasts implicitly; this extractor names the mistakes and the vocab
gaps. It runs on Claude Haiku in parallel with the tutor reply, so it never delays
the voice. Output follows the planned `record_feedback` shape, produced with
structured output rather than a tool call on the tutor LLM (which would sit inside
the spoken turn).
"""

import re
from enum import StrEnum
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from prompts import Level

FEEDBACK_MODEL = "claude-haiku-5-5"
FEEDBACK_MAX_TOKENS = 2000
FEEDBACK_REQUEST_EXTRAS: dict[str, Any] = {"output_config": {"effort": "low"}}
MAX_VOCAB_WORDS = 4  # longer "vocab" is a whole sentence, not a word to learn


class UserLanguage(StrEnum):
    SPANISH = "es"
    ENGLISH = "en"
    MIXED = "mixed"


class Correction(BaseModel):
    original: str = Field(description="The learner's wrong fragment, verbatim")
    corrected: str = Field(description="The same fragment, corrected")
    rule: str = Field(description="Why, in one short English line")


class VocabItem(BaseModel):
    spanish: str = Field(description="Spanish word or phrase; nouns with their article")
    english: str


class Feedback(BaseModel):
    """One learner turn's feedback (the `record_feedback` payload)."""

    corrections: list[Correction]
    new_vocab: list[VocabItem]
    english_gloss: str = Field(description="What the learner meant, in English")
    user_language: UserLanguage


_SYSTEM = """\
You analyse one turn of an English-speaking learner of Spain Spanish (CEFR {level}).
The turn is a speech-to-text transcript or typed text. Return:

corrections: real errors in the learner's Spanish only: grammar, agreement,
  conjugation, ser/estar, wrong word. "corrected" must differ from "original".
  Ignore missing accents, punctuation and capitalisation (transcription artefacts).
  Do not correct style or valid Latin American usage.
  Only flag forms that are wrong. If the learner's wording is grammatical, it is not
  an error, even when the tutor phrased it differently (era/estaba, fue/era).
  English words are never corrections: they go in new_vocab only.
  At most 3, most important first. Empty if the Spanish is correct.
new_vocab: single words or short phrases (max 4 words) the learner lacked: English
  words used inside a Spanish sentence, words they asked about ("how do you say..."),
  and for an English-only turn the 1-3 key content words (nouns, verbs, adjectives)
  needed to say it in Spanish. Never whole sentences. Never basic words a {level}
  learner knows (muy, mucho, bien, ser/estar forms, articles). Empty if nothing new.
english_gloss: what the learner meant, in English.
user_language: "es", "en" or "mixed".

Examples:
"Ayer compré un... umbrella muy bonito." → corrections: [] (the Spanish is correct);
  new_vocab: [{{"spanish": "el paraguas", "english": "umbrella"}}]; user_language: "mixed"
"El año pasado vivo en Londres." → corrections: [{{"original": "vivo",
  "corrected": "viví", "rule": "A past time marker (el año pasado) needs a past tense."}}];
  new_vocab: []; user_language: "es"
"""


class FeedbackExtractor:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client or anthropic.AsyncAnthropic()

    async def extract(self, learner: str, tutor_prev: str, level: Level) -> Feedback:
        context = f"Tutor said before: {tutor_prev}\n" if tutor_prev else ""
        response = await self._client.messages.parse(
            model=FEEDBACK_MODEL,
            max_tokens=FEEDBACK_MAX_TOKENS,
            system=_SYSTEM.format(level=level),
            messages=[{"role": "user", "content": f"{context}Learner said: {learner}"}],
            output_format=Feedback,
            **FEEDBACK_REQUEST_EXTRAS,
        )
        return _clean(response.parsed_output)


def _clean(fb: Feedback) -> Feedback:
    """Drop corrections Haiku adds despite the prompt (measured in evals/feedback.py).

    - no-ops: "en Utrecht" → "en Utrecht"
    - vocab gaps restated as corrections: "is a teacher" → "es profesora" when
      new_vocab already has profesora/teacher
    - whole sentences listed as vocab: "trabajo con ordenadores todo el día"
    """
    # "to travel" → {"travel"}; "teacher (female)" → {"teacher", "female"}
    gap_words = {
        w for v in fb.new_vocab for w in re.findall(r"[a-z']+", v.english.lower()) if len(w) > 2
    }

    def is_real(c: Correction) -> bool:
        if c.original.strip().lower() == c.corrected.strip().lower():
            return False
        original_words = set(re.findall(r"[a-z']+", c.original.lower()))
        return not (gap_words & original_words)

    vocab = [v for v in fb.new_vocab if len(v.spanish.split()) <= MAX_VOCAB_WORDS]
    corrections = [c for c in fb.corrections if is_real(c)]
    return fb.model_copy(update={"corrections": corrections, "new_vocab": vocab})
