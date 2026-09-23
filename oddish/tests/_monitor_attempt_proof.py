"""Building the attempt spec against a real Postgres — run as a subprocess by
``test_monitor_heartbeat_real_db.py`` (no ``test_`` prefix).

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
from oddish.db import get_session
from oddish.db.models import (
    Base,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
)
from oddish.workers.queue.monitor_attempt import build_attempt_spec
from oddish.workers.queue.monitor_token import MonitorAttempt


async def _main() -> int:
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    tables = [t for name, t in Base.metadata.tables.items() if name != "api_keys"]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )

    suffix = uuid.uuid4().hex[:10]
    trial_id = f"tr_{suffix}"
    async with get_session() as session:
        session.add(ExperimentModel(id=f"ex_{suffix}", name="monitor proof"))
        session.add(
            TaskModel(
                id=f"ta_{suffix}", name="hello", user="proof", task_path="/local/x"
            )
        )
        await session.flush()
        session.add(
            TrialModel(
                id=trial_id,
                name="proof-0",
                task_id=f"ta_{suffix}",
                experiment_id=f"ex_{suffix}",
                agent="oracle",
                model="nop_oracle",
                provider="default",
                queue_key="monitor-proof",
                origin=TrialOrigin.ODDISH,
                status=TrialStatus.RUNNING,
                attempts=2,
                harbor_config={"variant_id": "default"},
            )
        )

    spec = await build_attempt_spec(
        MonitorAttempt("wj_1", 3, "launcher-1", trial_id=trial_id)
    )
    checks = {
        "identity_from_the_credential": (spec.worker_job_id, spec.worker_job_attempt)
        == ("wj_1", 3),
        "trial_fields_from_the_row": (
            spec.agent,
            spec.model,
            spec.trial_attempt,
            spec.task_name,
            spec.harbor_config,
        )
        == ("oracle", "nop_oracle", 2, "hello", {"variant_id": "default"}),
        "local_only_task_has_nothing_to_fetch": spec.task_archive_url is None
        and spec.task_files == {},
    }
    try:
        await build_attempt_spec(MonitorAttempt("wj_1", 3, "w", trial_id="missing"))
        checks["missing_trial_is_an_error"] = False
    except ValueError:
        checks["missing_trial_is_an_error"] = True

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
