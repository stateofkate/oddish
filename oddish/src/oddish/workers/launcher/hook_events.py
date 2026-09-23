"""Harbor lifecycle events from the in-sandbox monitor.

The worker's hook handler (``trial_handler._handle_harbor_event``) writes the
trial's stage, reward, status and errors. It is reused unchanged: the monitor
sends a plain summary of each event and the API rebuilds the small part of the
hook event the handler reads.

The rebuilt event carries no environment object, provider or sandbox id. Inside
the sandbox Harbor drives a local Docker container, so those describe the inner
container; the real sandbox handle and its cost ledger belong to the launcher.
With no environment the handler also skips what only the monitor can do (probe
asset upload, starting the live tail).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from harbor.trial.hooks import TrialEvent

from oddish.mon.contract import summarize_hook_event  # noqa: F401 - re-exported
from oddish.workers.launcher.token import MonitorAttempt


def hook_event_from_summary(summary: dict[str, Any]) -> SimpleNamespace:
    timestamp = summary.get("timestamp")
    result = summary.get("result")
    rebuilt_result = None
    if result is not None:
        verifier = result.get("verifier_result")
        exc = result.get("exception_info")
        rebuilt_result = SimpleNamespace(
            verifier_result=(
                SimpleNamespace(rewards=dict(verifier.get("rewards") or {}))
                if verifier is not None
                else None
            ),
            exception_info=SimpleNamespace(**exc) if exc is not None else None,
        )
    return SimpleNamespace(
        event=TrialEvent(summary["event"]),
        timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
        result=rebuilt_result,
        environment=None,
        environment_provider=None,
        environment_external_id=None,
    )


async def apply_lifecycle_event(
    attempt: MonitorAttempt, summary: dict[str, Any]
) -> None:
    from oddish.workers.queue.trial_handler import _handle_harbor_event

    if attempt.trial_id is None:
        raise ValueError("This attempt does not drive a trial")
    await _handle_harbor_event(
        hook_event_from_summary(summary),  # type: ignore[arg-type]
        trial_id=attempt.trial_id,
        worker_id=attempt.worker_id,
        worker_job_id=attempt.worker_job_id,
        worker_job_attempt=attempt.attempt,
    )
