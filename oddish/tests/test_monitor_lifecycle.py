from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from harbor.trial.hooks import TrialEvent

from oddish.workers.queue import monitor_lifecycle, trial_handler


def _hook_event(**overrides):
    values = {
        "event": TrialEvent.END,
        "timestamp": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "environment": object(),
        "environment_provider": "docker",
        "environment_external_id": "inner-container",
        "result": SimpleNamespace(
            verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
            exception_info=SimpleNamespace(
                exception_type="AgentTimeoutError",
                exception_message="timed out",
                provider_error_code=None,
            ),
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_summary_round_trips_what_the_handler_reads():
    summary = monitor_lifecycle.summarize_hook_event(_hook_event())
    rebuilt = monitor_lifecycle.hook_event_from_summary(summary)

    assert rebuilt.event is TrialEvent.END
    assert rebuilt.timestamp == datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    assert rebuilt.result.verifier_result.rewards == {"reward": 1.0}
    assert trial_handler._is_agent_timeout_exception(rebuilt.result.exception_info)


def test_rebuilt_event_never_carries_the_inner_container_identity():
    summary = monitor_lifecycle.summarize_hook_event(_hook_event())
    assert "inner-container" not in str(summary)
    rebuilt = monitor_lifecycle.hook_event_from_summary(summary)
    assert rebuilt.environment is None
    assert rebuilt.environment_provider is None
    assert rebuilt.environment_external_id is None


def test_event_without_a_result_and_an_unknown_event():
    summary = monitor_lifecycle.summarize_hook_event(
        _hook_event(event=TrialEvent.AGENT_START, result=None)
    )
    assert monitor_lifecycle.hook_event_from_summary(summary).result is None
    with pytest.raises(ValueError):
        monitor_lifecycle.hook_event_from_summary({"event": "not-an-event"})
