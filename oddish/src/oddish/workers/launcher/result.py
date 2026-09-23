"""Durably accept the in-sandbox monitor's trial result.

The sandbox disappears once the monitor reports, so the outcome and the key of
its uploaded artifact archive are written onto the ``worker_jobs`` row before
the result is acknowledged. Settlement can then be built from the row alone.
Accepting is not settling: the job stays RUNNING until settlement finishes.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy import update

from oddish.db import get_session
from oddish.db.models import WorkerJobModel, WorkerJobStatus, utcnow
from oddish.workers.launcher.token import MonitorAttempt, MonitorGone

AcceptOutcome = Literal["accepted", "duplicate"]
logger = logging.getLogger(__name__)


async def accept_monitor_result(
    attempt: MonitorAttempt,
    *,
    outcome: dict[str, Any] | None,
    archive_key: str | None,
    execution_error: str | None = None,
    retryable: bool = True,
    monitor_stats: dict[str, int] | None = None,
) -> AcceptOutcome:
    """Store this attempt's result once.

    The monitor retries until it hears "accepted", so the same result can
    arrive again after a lost reply. A repeat delivery is acknowledged and
    changes nothing, which makes those retries safe.
    """
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.id == attempt.worker_job_id,
                WorkerJobModel.attempts == attempt.attempt,
                WorkerJobModel.status == WorkerJobStatus.RUNNING,
                WorkerJobModel.monitor_result_accepted_at.is_(None),
            )
            .values(
                monitor_result={
                    "attempt": attempt.attempt,
                    "outcome": outcome,
                    "archive_key": archive_key,
                    "execution_error": execution_error,
                    "retryable": retryable,
                    "monitor_stats": monitor_stats,
                },
                monitor_result_accepted_at=utcnow(),
            )
        )
        if result.rowcount == 1:
            logger.info(
                "metric=monitor_result outcome=accepted job_id=%s attempt=%s "
                "has_archive=%s execution_error=%s",
                attempt.worker_job_id,
                attempt.attempt,
                archive_key is not None,
                execution_error is not None,
            )
            return "accepted"
        job = await session.get(WorkerJobModel, attempt.worker_job_id)
        if (
            job is not None
            and job.attempts == attempt.attempt
            and job.monitor_result_accepted_at is not None
        ):
            return "duplicate"
    raise MonitorGone("This attempt can no longer report a result")
