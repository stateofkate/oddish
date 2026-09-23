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
from oddish.db.models import Base, WorkerJobModel, utcnow
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue import monitor_status_check, monitor_token
from oddish.workers.queue.monitor_result import accept_monitor_result
from oddish.workers.queue.monitor_settle import settle_monitor_result
from oddish.workers.queue.worker_job_single_job import (
    _ensure_handlers_registered,
    _record_outcome,
    claim_single_worker_job,
)
from sqlalchemy import update

QUEUE = "monitor-retry-proof"


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

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    await _conn.engine.dispose()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
