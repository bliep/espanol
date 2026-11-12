"""Which connection errors mean "the paused database is waking up, retry"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learner_store import is_resuming  # noqa: E402

TIMEOUT = (
    "Driver Error: Client unable to establish connection;"
    " DDBC Error: [Microsoft]TCP Provider: Timeout error [258]."
)
UNAVAILABLE = "Database 'tutor' on server 'x' is not currently available. (40613)"
FIREWALL = "Client with IP address '1.2.3.4' is not allowed to access the server. (40615)"
BAD_PASSWORD = "Login failed for user 'tutor_agent'. (18456)"


def test_resume_errors_retry() -> None:
    assert is_resuming(TIMEOUT)
    assert is_resuming(UNAVAILABLE)


def test_other_errors_fail_fast() -> None:
    assert not is_resuming(FIREWALL)
    assert not is_resuming(BAD_PASSWORD)
