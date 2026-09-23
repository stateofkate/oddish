"""A retried job owns none of the previous attempt's monitor state, against a
real Postgres — run as a subprocess by ``test_monitor_heartbeat_real_db.py``.

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import Settings

Settings.db_use_null_pool = True

import oddish.db.connection as _conn
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import (
    Base,
    QueueSlotModel,
    SandboxCapacityLeaseModel,
    WorkerJobModel,
    utcnow,
)
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue import cleanup, monitor_status_check, monitor_token
from oddish.workers.queue.monitor_result import accept_monitor_result
from oddish.workers.queue.monitor_settle import settle_monitor_result
from oddish.workers.queue.worker_job_single_job import (
    _ensure_handlers_registered,
    _record_outcome,
    claim_single_worker_job,
)
from sqlalchemy import update

QUEUE = "monitor-retry-proof"
REAP_QUEUE = "monitor-reap-proof"


async def _reapable_monitor_job(*, accepted: bool) -> tuple[str, str]:
    """A RUNNING, monitor-driven attempt 1 whose status check made it reapable."""
    job_id = "wj_" + uuid.uuid4().hex[:12]
    long_ago = utcnow() - timedelta(minutes=30)
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=job_id, kind=WorkerJobKind.TRIAL, queue_key=REAP_QUEUE,
                status=WorkerJobStatus.RUNNING, subject_table="trials",
                subject_id="tr_" + uuid.uuid4().hex[:12], payload={}, attempts=1,
                max_attempts=6, heartbeat_at=long_ago, claimed_at=long_ago,
                monitor_acked_at=long_ago,
                monitor_status_check_count=monitor_status_check.REAPABLE,
                monitor_status_checked_at=long_ago,
            )
        )  # fmt: skip
    credential = await monitor_token.mint_monitor_credential(job_id, 1)
    if accepted:
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == job_id)
                .values(
                    monitor_result={"attempt": 1, "outcome": {"reward": 1.0}},
                    monitor_result_accepted_at=utcnow(),
                )
            )
    return job_id, credential


class _AcceptAfterCandidateSelect:
    """The reap session, with a result accepted between its candidate SELECT
    and its UPDATE: the window a snapshot decision cannot close."""

    def __init__(self, session, accept) -> None:
        self._session, self._accept, self._fired = session, accept, False

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def execute(self, statement, params=None):
        result = await self._session.execute(statement, params)
        candidate_select = str(statement).lstrip().startswith("SELECT id, attempts")
        if candidate_select and not self._fired:
            self._fired = True
            await self._accept()
        return result


async def _job(job_id: str) -> WorkerJobModel:
    async with get_session() as session:
        return await session.get(WorkerJobModel, job_id)


async def _main() -> int:
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    tables = [t for name, t in Base.metadata.tables.items() if name != "api_keys"]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )

    # Attempt 1 checked in, reported a failure, took a strike, and was retried.
    job_id = "wj_" + uuid.uuid4().hex[:12]
    long_ago = utcnow() - timedelta(minutes=30)
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=job_id,
                kind=WorkerJobKind.TRIAL,
                queue_key=QUEUE,
                status=WorkerJobStatus.RETRYING,
                subject_table="trials",
                subject_id="tr_" + uuid.uuid4().hex[:12],
                # The previous launch's own keys must not survive the re-claim.
                payload={
                    "trial_id": "kept",
                    "monitor_cost": {"provider": "modal"},
                    "monitor_docker_image": "img:old",
                    "monitor_capacity": {"provider": "ec2", "slot": 0},
                },
                attempts=1,
                provider="modal",
                external_id="sb-of-attempt-1",
                monitor_token_hash="hash-of-attempt-1",
                monitor_token_expires_at=utcnow() + timedelta(hours=1),
                monitor_acked_at=long_ago,
                monitor_result={"attempt": 1, "outcome": None, "retryable": True},
                monitor_result_accepted_at=long_ago,
                monitor_status_check_count=2,
                monitor_status_checked_at=long_ago,
            )
        )

    checks: dict[str, bool] = {}
    _ensure_handlers_registered()
    claimed = await claim_single_worker_job(
        QUEUE, worker_id="launcher-2", queue_slot=0, harbor_variant_id=None,
        execution_lane=None,
    )  # fmt: skip
    job = await _job(job_id)
    checks["claimed_as_attempt_2"] = claimed is not None and job.attempts == 2
    checks["claim_cleared_monitor_state"] = all(
        value is None
        for value in (
            job.monitor_token_hash,
            job.monitor_acked_at,
            job.monitor_result,
            job.monitor_result_accepted_at,
            job.monitor_status_check_count,
            job.monitor_status_checked_at,
        )
    )
    checks["claim_dropped_the_previous_sandbox_handle"] = (
        job.provider is None and job.external_id is None
    )
    checks["claim_dropped_the_previous_launch_payload"] = (
        "monitor_cost" not in (job.payload or {})
        and "monitor_docker_image" not in (job.payload or {})
        and "monitor_capacity" not in (job.payload or {})
        and (job.payload or {}).get("trial_id") == "kept"
    )

    # The reconciler runs before the launcher has minted the new credential.
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(WorkerJobModel.id == job_id)
            .values(heartbeat_at=long_ago)
        )
    swept = await monitor_status_check.check_stale_monitor_jobs(stale_after_minutes=15)
    checks["reconciler_ignores_unminted_attempt"] = swept == {}

    # Attempt 2 reports success: accepted as new, not as attempt 1's duplicate.
    attempt_2 = await monitor_token.authorize_monitor(
        await monitor_token.mint_monitor_credential(job_id, 2)
    )
    accepted = await accept_monitor_result(
        attempt_2, outcome={"reward": 1.0}, archive_key=None
    )
    job = await _job(job_id)
    checks["attempt_2_result_is_new"] = accepted == "accepted"
    checks["stored_result_names_attempt_2"] = (job.monitor_result or {}).get(
        "attempt"
    ) == 2

    # A settle that observed attempt 1 arrives late: it must not touch attempt 2.
    late = await settle_monitor_result(job_id, attempt=1)
    job = await _job(job_id)
    checks["late_settle_of_attempt_1_is_a_noop"] = (
        late == "already_settled" and job.status == WorkerJobStatus.RUNNING
    )

    # A status check that observed attempt 2 finishes after attempt 3 started.
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(WorkerJobModel.id == job_id)
            .values(monitor_result=None, monitor_result_accepted_at=None)
        )

    async def _observe_while_the_job_moves_on(provider, external_id):
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == job_id)
                .values(attempts=3)
            )
        return monitor_status_check.Verdict.UNRESPONSIVE

    monitor_status_check.observe = _observe_while_the_job_moves_on
    await monitor_status_check.check_stale_monitor_jobs(stale_after_minutes=15)
    job = await _job(job_id)
    checks["late_status_check_gives_no_strike"] = (
        job.attempts == 3 and job.monitor_status_check_count is None
    )

    # Two settles of one result race: the outcome write has one winner, and only
    # the winner gets a status back, so the post-success hook fires once.
    raced_id = "wj_" + uuid.uuid4().hex[:12]
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=raced_id, kind=WorkerJobKind.TRIAL, queue_key=QUEUE,
                status=WorkerJobStatus.RUNNING, subject_table="trials",
                subject_id="tr_" + uuid.uuid4().hex[:12], payload={}, attempts=1,
                max_attempts=6, current_worker_id="launcher-1",
            )
        )  # fmt: skip
    written = await asyncio.gather(
        *(
            _record_outcome(
                job_id=raced_id, worker_id="launcher-1", outcome=JobOutcome.ok(),
                attempts=1, max_attempts=6,
            )
            for _ in range(2)
        )
    )  # fmt: skip
    checks["concurrent_settles_have_one_winner"] = (
        sum(status is not None for status in written) == 1
    )

    # The stale reap never takes an attempt whose result was accepted: that
    # result is settled, not re-scheduled. One without a result is reaped.
    kept_id, _ = await _reapable_monitor_job(accepted=True)
    reaped_id, reaped_credential = await _reapable_monitor_job(accepted=False)
    reaped_attempt = await monitor_token.authorize_monitor(reaped_credential)
    async with get_session() as session:
        await cleanup._reap_stale_worker_jobs(session, stale_after_minutes=15)
    kept, reaped = await _job(kept_id), await _job(reaped_id)
    checks["reap_skips_accepted_result"] = (
        kept.status == WorkerJobStatus.RUNNING
        and kept.attempts == 1
        and kept.stale_reaped_at is None
        and kept.monitor_result_accepted_at is not None
    )
    checks["reap_takes_reapable_without_result"] = (
        reaped.status == WorkerJobStatus.RETRYING
        and reaped.attempts == 1
        and reaped.stale_reaped_at is not None
    )
    # The monitor of the reaped attempt reports late: nothing is accepted.
    try:
        await accept_monitor_result(
            reaped_attempt, outcome={"reward": 1.0}, archive_key=None
        )
        checks["reaped_attempt_cannot_accept"] = False
    except monitor_token.MonitorGone:
        checks["reaped_attempt_cannot_accept"] = True

    # A result accepted after the reap chose its candidates, before its UPDATE:
    # the UPDATE's own fence keeps the job, however the two interleave.
    raced_reap_id, raced_credential = await _reapable_monitor_job(accepted=False)
    raced_attempt = await monitor_token.authorize_monitor(raced_credential)
    accepted_in_window: list[str] = []

    async def _accept_now() -> None:
        accepted_in_window.append(
            await accept_monitor_result(
                raced_attempt, outcome={"reward": 1.0}, archive_key=None
            )
        )

    async with get_session() as session:
        await cleanup._reap_stale_worker_jobs(
            _AcceptAfterCandidateSelect(session, _accept_now), stale_after_minutes=15
        )
    raced = await _job(raced_reap_id)
    checks["reap_update_fenced_on_result_accepted_in_window"] = (
        accepted_in_window == ["accepted"]
        and raced.status == WorkerJobStatus.RUNNING
        and raced.attempts == 1
        and raced.stale_reaped_at is None
        and raced.monitor_result_accepted_at is not None
    )

    # A reaped monitor attempt never settles, so the reap frees what its
    # hand-off kept: the launcher's queue slot and capacity lease. Both leases
    # are fresh, so neither the orphaned-slot sweep nor the capacity cleanup
    # (each behind a grace) can be what frees them; another worker's slot on
    # the same queue stays held.
    async def _still_unresponsive(provider, external_id):
        return monitor_status_check.Verdict.UNRESPONSIVE

    async def _no_tag_owners(_session):  # needs the backend's users table
        return 0

    monitor_status_check.observe = _still_unresponsive
    cleanup.sweep_orphaned_tag_owners = _no_tag_owners
    released_id, _ = await _reapable_monitor_job(accepted=False)
    fresh_until = utcnow() + timedelta(hours=12)
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(WorkerJobModel.id == released_id)
            .values(
                current_worker_id="launcher-r",
                current_queue_slot=0,
                payload={"monitor_capacity": {"provider": "ec2", "slot": 7}},
            )
        )
        session.add_all(
            [
                QueueSlotModel(
                    queue_key=REAP_QUEUE, slot=0, locked_by="launcher-r",
                    locked_at=utcnow(), locked_until=fresh_until,
                ),
                QueueSlotModel(
                    queue_key=REAP_QUEUE, slot=1, locked_by="other-worker",
                    locked_at=utcnow(), locked_until=fresh_until,
                ),
                SandboxCapacityLeaseModel(
                    provider="ec2", slot=7, locked_by="launcher-r",
                    worker_job_id=released_id, locked_at=utcnow(),
                    locked_until=fresh_until,
                ),
            ]
        )  # fmt: skip
    await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
    released = await _job(released_id)
    async with get_session() as session:
        own_slot = await session.get(QueueSlotModel, (REAP_QUEUE, 0))
        other_slot = await session.get(QueueSlotModel, (REAP_QUEUE, 1))
        lease = await session.get(SandboxCapacityLeaseModel, ("ec2", 7))
    checks["cleanup_reaped_the_handed_off_attempt"] = (
        released.status == WorkerJobStatus.RETRYING
        and released.stale_reaped_at is not None
        and released.current_worker_id is None
    )
    checks["reap_released_the_handed_off_queue_slot"] = (
        own_slot.locked_by is None
        and own_slot.locked_until is None
        and own_slot.locked_at is None
    )
    checks["reap_kept_another_workers_slot"] = other_slot.locked_by == "other-worker"
    checks["reap_released_the_handed_off_capacity_lease"] = (
        lease.locked_by is None and lease.worker_job_id is None
    )
    checks["reap_revoked_the_attempts_token"] = (
        released.monitor_token_revoked_at is not None
    )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    await _conn.engine.dispose()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
