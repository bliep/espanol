"""English translation of a tutor line, on demand (web "translate" button).

Haiku, ~1 s; cached per line so repeated taps cost nothing.
"""

from typing import Any

import anthropic

from costs import CostMeter

TRANSLATE_MODEL = "claude-haiku-5-5"
TRANSLATE_MAX_TOKENS = 500
TRANSLATE_REQUEST_EXTRAS: dict[str, Any] = {"output_config": {"effort": "low"}}

_SYSTEM = (
    "Translate the Spanish tutor's line into natural English for a learner. "
    "Reply with the translation only."
)


class SentenceTranslator:
    def __init__(
        self, client: anthropic.AsyncAnthropic | None = None, meter: CostMeter | None = None
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()
        self._meter = meter
        self._cache: dict[str, str] = {}

    async def translate(self, spanish: str) -> str:
        key = spanish.strip()
        if key in self._cache:
            return self._cache[key]

        response = await self._client.messages.create(
            model=TRANSLATE_MODEL,
            max_tokens=TRANSLATE_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": key}],
            **TRANSLATE_REQUEST_EXTRAS,
        )
        if self._meter:
            self._meter.add_claude(TRANSLATE_MODEL, response.usage)

        english = next(b.text for b in response.content if b.type == "text").strip()
        self._cache[key] = english
        return english
