"""``is_cancelled_or_exhausted_trial`` decides whether a late worker result or
Harbor hook may still update a trial. Every cancel path writes FAILED with
``max_attempts = attempts``; user cancels also write USER_CANCELLED_MESSAGE.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    QUOTA_CANCELLED_MESSAGE,
    USER_CANCELLED_MESSAGE,
    TrialStatus,
    is_cancelled_or_exhausted_trial,
)


def _trial(status, *, attempts=1, max_attempts=6, error_message=None):
    return SimpleNamespace(
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        error_message=error_message,
    )


def test_user_cancel_before_first_attempt():
    trial = _trial(
        TrialStatus.FAILED,
        attempts=0,
        max_attempts=0,
        error_message=USER_CANCELLED_MESSAGE,
    )
    assert is_cancelled_or_exhausted_trial(trial)


def test_quota_cancel_is_caught_by_attempts():
    trial = _trial(
        TrialStatus.FAILED,
        attempts=2,
        max_attempts=2,
        error_message=QUOTA_CANCELLED_MESSAGE,
    )
    assert is_cancelled_or_exhausted_trial(trial)


def test_failed_on_final_attempt():
    assert is_cancelled_or_exhausted_trial(
        _trial(TrialStatus.FAILED, attempts=6, max_attempts=6)
    )


def test_live_or_retryable_trials_stay_open():
    assert not is_cancelled_or_exhausted_trial(_trial(TrialStatus.RUNNING))
    assert not is_cancelled_or_exhausted_trial(_trial(TrialStatus.RETRYING))
    assert not is_cancelled_or_exhausted_trial(
        _trial(TrialStatus.FAILED, attempts=2, max_attempts=6)
    )
