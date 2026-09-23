"""Monitor token + heartbeat against a real Postgres — run as a subprocess by
``test_monitor_heartbeat_real_db.py`` (no ``test_`` prefix, so pytest never
collects it directly). Same harness shape as ``_offmodal_proof.py``.

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
from oddish.workers.queue import monitor_token
from oddish.workers.queue.monitor_heartbeat import (
    record_monitor_heartbeat,
)


async def _raises(exc_type, credential: str) -> bool:
    try:
        await monitor_token.authorize_monitor(credential)
    except exc_type:
        return True
    return False


async def _main() -> int:
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    tables = [t for name, t in Base.metadata.tables.items() if name != "api_keys"]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )

    job_id = "wj_" + uuid.uuid4().hex[:12]
    stale = utcnow() - timedelta(minutes=20)
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
                current_queue_slot=3,
                heartbeat_at=stale,
            )
        )

    checks: dict[str, bool] = {}
    credential = await monitor_token.mint_monitor_credential(job_id, 1)
    attempt = await monitor_token.authorize_monitor(credential)
    checks["authorized_as_launcher_worker_id"] = (
        attempt.worker_id == "launcher-1" and attempt.queue_slot == 3
    )

    checks["heartbeat_still_owned"] = await record_monitor_heartbeat(attempt)
    async with get_session() as session:
        row = await session.get(WorkerJobModel, job_id)
        checks["heartbeat_at_advanced"] = row.heartbeat_at > stale
        checks["raw_token_not_stored"] = credential.rsplit(".", 1)[1] not in (
            row.monitor_token_hash or ""
        )

    checks["wrong_token_unauthorized"] = await _raises(
        monitor_token.MonitorUnauthorized, f"{job_id}.1.not-the-token"
    )
    raw = credential.rsplit(".", 1)[1]
    checks["older_attempt_gone"] = await _raises(
        monitor_token.MonitorGone, f"{job_id}.0.{raw}"
    )

    await monitor_token.revoke_monitor_credential(job_id, attempt=0)
    checks["older_attempt_revoke_spares_current"] = not await _raises(
        monitor_token.MonitorGone, credential
    )
    await monitor_token.revoke_monitor_credential(job_id, attempt=1)
    checks["revoked_gone"] = await _raises(monitor_token.MonitorGone, credential)

    async with get_session() as session:
        row = await session.get(WorkerJobModel, job_id)
        row.status = WorkerJobStatus.RETRYING
    try:
        await monitor_token.mint_monitor_credential(job_id, 1)
        checks["no_token_for_inactive_attempt"] = False
    except RuntimeError:
        checks["no_token_for_inactive_attempt"] = True
    checks["heartbeat_after_retry_not_owned"] = not await record_monitor_heartbeat(
        attempt
    )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
