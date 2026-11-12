"""Running cost of a session in USD, from usage counts and a price table.

  pipeline (LiveKit session.usage) ── Sonnet tokens, Deepgram audio, Cartesia characters
  direct calls ── add_claude(): feedback, summary, drills, translation (Haiku / Sonnet)
               ── add_azure(): pronunciation audio
  wall clock   ── LiveKit agent + WebRTC minutes
                         │
                         ▼
               breakdown(): per provider, total, per minute

Report over stored sessions: cd agent && uv run python costs.py [--days 30]

Prices checked 2026-11-09. Update them here when a provider or plan changes.
"""

import argparse
import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession
from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage
from pydantic import BaseModel

MTOK = 1_000_000
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class ClaudePrice:
    """USD per million tokens."""

    input: float
    cache_write: float  # 5-minute cache
    cache_read: float
    output: float


# platform.claude.com/docs/en/about-claude/pricing (Haiku: prompts up to 100k tokens)
CLAUDE_PRICES: dict[str, ClaudePrice] = {
    "claude-sonnet-5-5": ClaudePrice(input=2.0, cache_write=2.5, cache_read=0.10, output=10.0),
    "claude-haiku-5-5": ClaudePrice(input=0.10, cache_write=0.125, cache_read=0.01, output=0.50),
}
DEEPGRAM_PER_MINUTE = 0.0092  # deepgram.com/pricing: Nova-3 multilingual, streaming, PAYG
CARTESIA_PER_CHARACTER = 5 / 100_000  # cartesia.ai/pricing: Pro, $5 for 100k credits, 1/char
AZURE_PER_HOUR = 1.0 + 0.30  # estimate: real-time STT $1/h + pronunciation add-on $0.30/h
LIVEKIT_AGENT_PER_MINUTE = 0.01  # livekit.com/pricing: agent session minutes beyond plan
LIVEKIT_WEBRTC_PER_MINUTE = 0.0005  # learner's WebRTC connection minutes beyond plan


class CostBreakdown(BaseModel):
    """Wire format of the session_cost RPC; also stored in sessions.cost."""

    minutes: float
    claude: float
    deepgram: float
    cartesia: float
    azure: float
    livekit: float
    total: float
    per_minute: float


class CostMeter:
    def __init__(self) -> None:
        self._start = time.monotonic()
        self._session: AgentSession | None = None
        self._claude = 0.0  # direct calls, USD
        self._azure_seconds = 0.0
        self._extra_tts_characters = 0  # TTS outside the session pipeline (slow replay)

    def attach(self, session: AgentSession) -> None:
        self._session = session

    def add_claude(self, model: str, usage: Any) -> None:
        """usage: an Anthropic `Usage` (input_tokens excludes cache reads and writes)."""
        price = CLAUDE_PRICES[model]
        self._claude += (
            usage.input_tokens * price.input
            + (usage.cache_creation_input_tokens or 0) * price.cache_write
            + (usage.cache_read_input_tokens or 0) * price.cache_read
            + usage.output_tokens * price.output
        ) / MTOK

    def add_azure(self, seconds: float) -> None:
        self._azure_seconds += seconds

    def add_tts(self, characters: int) -> None:
        self._extra_tts_characters += characters

    def breakdown(self) -> CostBreakdown:
        claude, deepgram = self._claude, 0.0
        cartesia = self._extra_tts_characters * CARTESIA_PER_CHARACTER
        for usage in self._session.usage.model_usage if self._session else []:
            match usage:
                case LLMModelUsage() if usage.model in CLAUDE_PRICES:
                    claude += _pipeline_llm_cost(usage)
                case STTModelUsage():
                    deepgram += usage.audio_duration / SECONDS_PER_MINUTE * DEEPGRAM_PER_MINUTE
                case TTSModelUsage():
                    cartesia += usage.characters_count * CARTESIA_PER_CHARACTER

        minutes = (time.monotonic() - self._start) / SECONDS_PER_MINUTE
        azure = self._azure_seconds / SECONDS_PER_HOUR * AZURE_PER_HOUR
        livekit = minutes * (LIVEKIT_AGENT_PER_MINUTE + LIVEKIT_WEBRTC_PER_MINUTE)
        total = claude + deepgram + cartesia + azure + livekit
        return CostBreakdown(
            minutes=round(minutes, 2),
            claude=round(claude, 4),
            deepgram=round(deepgram, 4),
            cartesia=round(cartesia, 4),
            azure=round(azure, 4),
            livekit=round(livekit, 4),
            total=round(total, 4),
            per_minute=round(total / minutes, 4) if minutes else 0.0,
        )


def _pipeline_llm_cost(usage: LLMModelUsage) -> float:
    """LiveKit's input_tokens include cache reads and writes; split them out."""
    price = CLAUDE_PRICES[usage.model]
    fresh = usage.input_tokens - usage.input_cached_tokens - usage.input_cache_creation_tokens
    return (
        fresh * price.input
        + usage.input_cache_creation_tokens * price.cache_write
        + usage.input_cached_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / MTOK


async def report(days: int) -> None:
    """Totals over stored sessions (all learners)."""
    from dotenv import load_dotenv

    from learner_store import DbLogin, LearnerStore, connection_string

    load_dotenv(Path(__file__).parent.parent / ".env")
    store = await LearnerStore.connect(connection_string(os.environ, DbLogin.AGENT))
    costs = await store.session_costs(datetime.now(UTC) - timedelta(days=days))
    await store.close()

    breakdowns = [CostBreakdown(**c) for c in costs]
    minutes = sum(b.minutes for b in breakdowns)
    total = sum(b.total for b in breakdowns)
    print(f"last {days} days: {len(breakdowns)} sessions, {minutes:.1f} min, ${total:.2f}")
    if not minutes:
        return

    print(f"per minute: ${total / minutes:.4f}")
    for provider in ("claude", "deepgram", "cartesia", "azure", "livekit"):
        amount = sum(getattr(b, provider) for b in breakdowns)
        print(f"  {provider:9s} ${amount:.3f} ({amount / total:.0%})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    asyncio.run(report(parser.parse_args().days))
