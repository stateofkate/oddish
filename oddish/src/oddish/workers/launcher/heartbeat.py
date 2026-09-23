"""The monitor's heartbeat, written exactly as the worker writes it today."""

from __future__ import annotations

from oddish.workers.launcher.token import MonitorAttempt
from oddish.workers.queue.trial_handler import _touch_trial_execution
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job


async def record_monitor_heartbeat(
    attempt: MonitorAttempt,
    *,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
) -> bool:
    """Touch the trial row, then the worker-job row and its capacity lease.

    Same two calls, same order and same owner id as the worker's heartbeat
    loop. Returns whether this attempt still owns the running job. Raises
    ``SandboxCapacityLeaseLostError`` when the capacity lease is gone.
    """
    if attempt.trial_id is not None:
        await _touch_trial_execution(
            trial_id=attempt.trial_id,
            worker_id=attempt.worker_id,
            queue_slot=attempt.queue_slot,
            pending_failure_count=pending_failure_count,
            pending_last_error=pending_last_error,
        )
    return await heartbeat_worker_job(
        attempt.worker_job_id,
        current_worker_id=attempt.worker_id,
        pending_failure_count=pending_failure_count,
        pending_last_error=pending_last_error,
    )
