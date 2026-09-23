"""One reconciler pass spends a bounded time on monitors and rotates through
them, against a real Postgres — run by ``test_monitor_heartbeat_real_db.py``.

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import timedelta

import _harness  # sets the path and the null pool first
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import WorkerJobModel, utcnow
from oddish.workers.launcher import reaper as check
from sqlalchemy import func, select


async def _seed(count: int, queue: str) -> None:
    stale = utcnow() - timedelta(minutes=30)
    async with get_session() as session:
        for _ in range(count):
            session.add(
                WorkerJobModel(
                    id="wj_" + uuid.uuid4().hex[:12], kind=WorkerJobKind.TRIAL,
                    queue_key=queue, status=WorkerJobStatus.RUNNING,
                    subject_table="trials", subject_id="tr_" + uuid.uuid4().hex[:12],
                    payload={}, attempts=1, monitor_token_hash="h",
                    claimed_at=utcnow(), heartbeat_at=stale, provider="modal",
                    external_id="sb-" + uuid.uuid4().hex[:8],
                )
            )  # fmt: skip


async def _checked(queue: str) -> int:
    async with get_session() as session:
        return await session.scalar(
            select(func.count())
            .select_from(WorkerJobModel)
            .where(
                WorkerJobModel.queue_key == queue,
                WorkerJobModel.monitor_status_checked_at.is_not(None),
            )
        )


async def _main() -> int:
    await _harness.boot()
    checks: dict[str, bool] = {}

    async def _slow_unresponsive(provider, external_id):
        await asyncio.sleep(1.0)
        return check.Verdict.UNRESPONSIVE

    check.observe = _slow_unresponsive

    # 20 unresponsive monitors are checked side by side, not one after another.
    await _seed(20, "budget-a")
    started = time.monotonic()
    counts = await check.check_stale_monitor_jobs(stale_after_minutes=15)
    checks["twenty_unresponsive_fit_one_pass"] = (
        counts.get("unresponsive") == 20 and time.monotonic() - started < 8
    )

    # More new jobs than a batch: one pass takes a batch, the next takes the rest
    # before it returns to anything already checked.
    await _seed(65, "budget-b")
    await check.check_stale_monitor_jobs(stale_after_minutes=15)
    checks["a_pass_takes_one_batch"] = await _checked("budget-b") == check.PASS_BATCH
    await check.check_stale_monitor_jobs(stale_after_minutes=15)
    checks["rotation_reaches_every_job"] = await _checked("budget-b") == 65

    # Out of budget: the pass ends on time and defers the rest.
    check.PASS_BUDGET_SECONDS = 0.5
    started = time.monotonic()
    counts = await check.check_stale_monitor_jobs(stale_after_minutes=15)
    checks["pass_stops_at_its_budget"] = (
        counts.get("deferred", 0) > 0 and time.monotonic() - started < 4
    )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    await _harness.dispose()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
