"""The server's verdict labels and cancellation errors match the dashboard's.

Delivery checks carry label text built on the server; the dashboard builds the
same labels from frontend/src/lib/verdict-state.ts. A task must read the same
in both places.
"""

import re
from pathlib import Path

import pytest

from oddish.core.quota_enforcement import QUOTA_CANCELLED_MESSAGE
from oddish.core.verdict_state import (
    CANCELLED_VERDICT_ERRORS,
    VERDICT_STATE_LABELS,
    is_cancelled_verdict,
)
from oddish.queue import USER_CANCELLED_MESSAGE

FRONTEND = Path(__file__).resolve().parents[2] / "frontend/src/lib/verdict-state.ts"


def test_cancellation_errors_are_the_ones_cancel_verdict_writes():
    assert CANCELLED_VERDICT_ERRORS == {USER_CANCELLED_MESSAGE, QUOTA_CANCELLED_MESSAGE}
    assert is_cancelled_verdict(USER_CANCELLED_MESSAGE)
    assert not is_cancelled_verdict("worker crashed")
    assert not is_cancelled_verdict(None)


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend checkout not present")
def test_labels_and_cancellation_errors_match_the_dashboard():
    source = FRONTEND.read_text()
    table = source[
        source.index("VERDICT_STATE_LABELS = {") : source.index("} as const")
    ]
    assert dict(re.findall(r'(\w+): "([^"]+)"', table)) == VERDICT_STATE_LABELS
    cancel = source[source.index("function isCancelledVerdict") :]
    cancel = cancel[: cancel.index("\n}\n")]
    assert set(re.findall(r'error === "([^"]+)"', cancel)) == CANCELLED_VERDICT_ERRORS
