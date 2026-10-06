"""Renew monitor ownership before updating secondary trial metadata."""

from __future__ import annotations

import logging

from oddish.workers.launcher.token import MonitorAttempt
from oddish.workers.queue.trial_steps import touch_trial_execution
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job


logger = logging.getLogger(__name__)


async def record_monitor_heartbeat(
    attempt: MonitorAttempt,
    *,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
) -> bool:
    """Commit job and capacity renewal even when the trial row is locked.

    A skipped trial update is retried on the next beat. Actual ownership loss
    still returns False (or raises for a lost required capacity lease).
    """
    still_owned = await heartbeat_worker_job(
        attempt.worker_job_id,
        current_worker_id=attempt.worker_id,
        current_attempt=attempt.attempt,
        pending_failure_count=pending_failure_count,
        pending_last_error=pending_last_error,
    )
    if not still_owned:
        return False
    if attempt.trial_id is not None:
        updated = await touch_trial_execution(
            trial_id=attempt.trial_id,
            worker_id=attempt.worker_id,
            queue_slot=attempt.queue_slot,
            skip_locked=True,
            pending_failure_count=pending_failure_count,
            pending_last_error=pending_last_error,
        )
        if not updated:
            logger.info(
                "metric=monitor_trial_heartbeat outcome=skipped job_id=%s attempt=%s",
                attempt.worker_job_id,
                attempt.attempt,
            )
    return True
