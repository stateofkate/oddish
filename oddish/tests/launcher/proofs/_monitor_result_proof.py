"""Accepting the monitor's result against a real Postgres — run as a subprocess
by ``test_monitor_heartbeat_real_db.py`` (no ``test_`` prefix).

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import _harness  # sets the path and the null pool first
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import WorkerJobModel
from oddish.workers.launcher import token as monitor_token
from oddish.workers.launcher.result import accept_monitor_result


async def _main() -> int:
    await _harness.boot()
    job_id = "wj_" + uuid.uuid4().hex[:12]
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=job_id,
                kind=WorkerJobKind.TRIAL,
                queue_key="monitor-proof",
                status=WorkerJobStatus.RUNNING,
                subject_table="trials",
                subject_id="tr_" + uuid.uuid4().hex[:12],
                payload={},
                attempts=1,
                current_worker_id="launcher-1",
            )
        )
    attempt = await monitor_token.authorize_monitor(
        await monitor_token.mint_monitor_credential(job_id, 1)
    )

    checks: dict[str, bool] = {}
    first = await accept_monitor_result(
        attempt, outcome={"reward": 1.0}, archive_key="tasks/t/trials/x/a.tar.gz"
    )
    second = await accept_monitor_result(
        attempt, outcome={"reward": 0.0}, archive_key=None
    )
    async with get_session() as session:
        row = await session.get(WorkerJobModel, job_id)
        checks["first_result_accepted"] = first == "accepted"
        checks["second_delivery_is_duplicate"] = second == "duplicate"
        checks["first_result_not_overwritten"] = (
            row.monitor_result["outcome"] == {"reward": 1.0}
            and row.monitor_result["archive_key"] == "tasks/t/trials/x/a.tar.gz"
        )
        checks["job_still_running_until_settled"] = (
            row.status == WorkerJobStatus.RUNNING
        )
        row.monitor_result = None
        row.monitor_result_accepted_at = None
        row.status = WorkerJobStatus.RETRYING

    try:
        await accept_monitor_result(attempt, outcome={"reward": 1.0}, archive_key=None)
        checks["retried_attempt_cannot_report"] = False
    except monitor_token.MonitorGone:
        checks["retried_attempt_cannot_report"] = True

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
