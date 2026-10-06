"""Real Postgres: a locked trial must not suppress monitor ownership renewal."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import _harness
from sqlalchemy import select

from oddish.db import get_session
from oddish.db.models import (
    ExperimentModel,
    SandboxCapacityLeaseModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    utcnow,
)
from oddish.workers.launcher.heartbeat import record_monitor_heartbeat
from oddish.workers.launcher.token import MonitorAttempt
from oddish.workers.queue.worker_job_single_job import SandboxCapacityLeaseLostError


async def _main() -> int:
    await _harness.boot()
    stale = utcnow() - timedelta(minutes=20)
    async with get_session() as session:
        session.add(ExperimentModel(id="ex-lock", name="heartbeat lock proof"))
        session.add(TaskModel(id="ta-lock", name="proof", user="proof", task_path="/x"))
        await session.flush()
        session.add(
            TrialModel(
                id="tr-lock",
                name="proof-0",
                task_id="ta-lock",
                experiment_id="ex-lock",
                agent="oracle",
                provider="default",
                queue_key="monitor-proof",
                origin=TrialOrigin.ODDISH,
                status=TrialStatus.RUNNING,
                current_worker_id="owner",
                current_queue_slot=3,
                heartbeat_at=stale,
            )
        )
        session.add(
            WorkerJobModel(
                id="wj-lock",
                kind=WorkerJobKind.TRIAL,
                queue_key="monitor-proof",
                status=WorkerJobStatus.RUNNING,
                subject_table="trials",
                subject_id="tr-lock",
                payload={},
                attempts=1,
                current_worker_id="owner",
                current_queue_slot=3,
                heartbeat_at=stale,
                execution_lane="ec2_trial",
                monitor_status_check_count=2,
            )
        )
        await session.flush()
        session.add(
            SandboxCapacityLeaseModel(
                provider="ec2",
                slot=0,
                locked_by="owner",
                worker_job_id="wj-lock",
                locked_at=stale,
                locked_until=stale,
            )
        )

    attempt = MonitorAttempt("wj-lock", 1, "owner", trial_id="tr-lock", queue_slot=3)
    checks = {}
    # The separate session holds this lock throughout the request AND the reads
    # below. Success cannot depend on the competing transaction releasing it.
    async with get_session() as blocker:
        await blocker.scalar(
            select(TrialModel).where(TrialModel.id == "tr-lock").with_for_update()
        )
        checks["locked_trial_does_not_block_heartbeat"] = await asyncio.wait_for(
            record_monitor_heartbeat(
                attempt, pending_failure_count=2, pending_last_error="missed"
            ),
            timeout=2,
        )
        async with get_session() as observer:
            job = await observer.get(WorkerJobModel, "wj-lock")
            lease = await observer.get(SandboxCapacityLeaseModel, ("ec2", 0))
            trial = await observer.get(TrialModel, "tr-lock")
            checks["renewal_committed_while_trial_locked"] = (
                job.heartbeat_at > stale and lease.locked_until > utcnow()
            )
            checks["locked_trial_unchanged"] = trial.heartbeat_at == stale
            checks["job_diagnostics_persisted"] = (
                job.heartbeat_failure_count == 2
                and job.last_heartbeat_error == "missed"
            )
            checks["stale_monitor_checks_reset"] = (
                job.monitor_status_check_count is None
            )

    checks["later_heartbeat_succeeds"] = await record_monitor_heartbeat(attempt)
    async with get_session() as session:
        trial = await session.get(TrialModel, "tr-lock")
        checks["trial_mirror_catches_up"] = trial.heartbeat_at > stale
        job = await session.get(WorkerJobModel, "wj-lock")
        # Simulate authorization preceding a retry. Reuse the owner deliberately:
        # matching only worker_id must not allow the old attempt to renew it.
        job.attempts = 2
        job.heartbeat_at = stale
        trial.heartbeat_at = stale
        lease = await session.get(SandboxCapacityLeaseModel, ("ec2", 0))
        lease.locked_until = stale

    for owner in ("another-owner", None):
        checks[f"wrong_owner_rejected_{owner}"] = not await record_monitor_heartbeat(
            MonitorAttempt("wj-lock", 2, owner, trial_id="tr-lock", queue_slot=3)
        )

    checks["superseded_attempt_rejected"] = not await record_monitor_heartbeat(
        attempt, pending_failure_count=1, pending_last_error="old attempt"
    )
    async with get_session() as session:
        job = await session.get(WorkerJobModel, "wj-lock")
        trial = await session.get(TrialModel, "tr-lock")
        lease = await session.get(SandboxCapacityLeaseModel, ("ec2", 0))
        checks["superseded_attempt_writes_nothing"] = (
            job.heartbeat_at == trial.heartbeat_at == lease.locked_until == stale
            and job.heartbeat_failure_count == 2
        )
        job.attempts = 1
        job.status = WorkerJobStatus.RETRYING
    checks["inactive_job_rejected"] = not await record_monitor_heartbeat(attempt)

    async with get_session() as session:
        job = await session.get(WorkerJobModel, "wj-lock")
        job.status = WorkerJobStatus.RUNNING
        lease = await session.get(SandboxCapacityLeaseModel, ("ec2", 0))
        lease.locked_by = "replacement-owner"
    try:
        await record_monitor_heartbeat(attempt, pending_failure_count=1)
        checks["lost_capacity_rejected"] = False
    except SandboxCapacityLeaseLostError:
        checks["lost_capacity_rejected"] = True
    async with get_session() as session:
        job = await session.get(WorkerJobModel, "wj-lock")
        lease = await session.get(SandboxCapacityLeaseModel, ("ec2", 0))
        trial = await session.get(TrialModel, "tr-lock")
        checks["lost_capacity_rolls_back_job_renewal"] = (
            job.heartbeat_at == stale
            and job.heartbeat_failure_count == 2
            and trial.heartbeat_at == stale
            and lease.locked_until == stale
        )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    await _harness.dispose()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
