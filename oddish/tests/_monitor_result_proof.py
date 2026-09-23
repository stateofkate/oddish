"""Accepting the monitor's result against a real Postgres — run as a subprocess
by ``test_monitor_heartbeat_real_db.py`` (no ``test_`` prefix).

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import Settings

Settings.db_use_null_pool = True

import oddish.db.connection as _conn
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import Base, WorkerJobModel
from oddish.workers.queue import monitor_token
from oddish.workers.queue.monitor_result import accept_monitor_result


async def _main() -> int:
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    tables = [t for name, t in Base.metadata.tables.items() if name != "api_keys"]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )

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
