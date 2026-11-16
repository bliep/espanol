"""Per-model cost lines: split by model, and they add up to the provider totals."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from costs import AZURE_LINE, LIVEKIT_LINE, SLOW_REPLAY_LINE, CostMeter  # noqa: E402

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def test_lines_per_model() -> None:
    meter = CostMeter()
    meter.add_claude(SONNET, _usage(1_000_000, 0))  # $2.00
    meter.add_claude(HAIKU, _usage(1_000_000, 0))  # $0.10
    meter.add_claude(HAIKU, _usage(0, 1_000_000))  # $0.50
    meter.add_tts(100_000)  # $5.00
    meter.add_azure(3600)  # $1.30

    cost = meter.breakdown()

    assert cost.models[SONNET] == 2.0
    assert cost.models[HAIKU] == 0.6
    assert cost.claude == 2.6
    assert cost.models[SLOW_REPLAY_LINE] == cost.cartesia == 5.0
    assert cost.models[AZURE_LINE] == cost.azure == 1.3
    assert cost.models[LIVEKIT_LINE] == cost.livekit
    assert round(sum(cost.models.values()), 4) == cost.total
