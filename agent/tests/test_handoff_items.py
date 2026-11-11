"""History listeners must accept non-message items.

A lesson-mode switch adds an AgentHandoff item to the chat history; it has no role
or text. Listeners on conversation_item_added must skip it without raising.
"""

import sys
from pathlib import Path

from livekit.agents import ConversationItemAddedEvent, llm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latency import LatencyLogger  # noqa: E402
from prompts import Level  # noqa: E402
from turn_recorder import TurnRecorder  # noqa: E402

HANDOFF = ConversationItemAddedEvent(item=llm.AgentHandoff(new_agent_id="drill"))


def test_latency_logger_skips_handoff() -> None:
    LatencyLogger()._on_item(HANDOFF)  # noqa: SLF001


def test_turn_recorder_skips_handoff(tmp_path: Path) -> None:
    TurnRecorder(Level.A2, folder=tmp_path)._on_item(HANDOFF)  # noqa: SLF001
    assert not list(tmp_path.iterdir())
