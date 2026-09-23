"""The live-tail persist tick against a real Postgres — run as a subprocess by
``test_monitor_heartbeat_real_db.py`` (no ``test_`` prefix). This is the
function both the worker's tailer and the monitor's events route call.

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import _harness  # sets the path and the null pool first
from oddish.db import get_session
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialEventModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    utcnow,
)
from oddish.workers.harbor.live_tail_db import persist_tick_to_database
from sqlalchemy import func, select


async def _main() -> int:
    await _harness.boot()
    suffix = uuid.uuid4().hex[:10]
    trial_id = f"tr_{suffix}"
    async with get_session() as session:
        session.add(ExperimentModel(id=f"ex_{suffix}", name="monitor proof"))
        session.add(
            TaskModel(id=f"ta_{suffix}", name="proof", user="proof", task_path="/x")
        )
        await session.flush()
        session.add(
            TrialModel(
                id=trial_id,
                name="proof-0",
                task_id=f"ta_{suffix}",
                experiment_id=f"ex_{suffix}",
                agent="oracle",
                provider="default",
                queue_key="monitor-proof",
                origin=TrialOrigin.ODDISH,
                status=TrialStatus.RUNNING,
            )
        )

    events = [
        {"seq": 1, "kind": "text", "payload": {"text": "one"}},
        {"seq": 2, "kind": "text", "payload": {"text": "two"}},
    ]
    usage = {
        "input_tokens": 10,
        "cache_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 5,
        "cost_usd": 0.25,
    }
    args = dict(trial_id=trial_id, attempt=0, org_id=None, billed_user_id=None)
    checks: dict[str, bool] = {}

    first = await persist_tick_to_database(
        events=events, checkpoint_values=usage, **args
    )
    checks["rows_flushed_and_checkpoint_written"] = (
        first.flushed == 2 and first.checkpoint_written and not first.trial_finished
    )
    # A retried delivery of the same rows must not duplicate them.
    await persist_tick_to_database(events=events, checkpoint_values=None, **args)
    async with get_session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(TrialEventModel)
            .where(TrialEventModel.trial_id == trial_id)
        )
        trial = await session.get(TrialModel, trial_id)
        checks["retry_does_not_duplicate_rows"] = count == 2
        checks["usage_checkpoint_on_trial"] = (
            trial.input_tokens == 10 and float(trial.cost_usd) == 0.25
        )
        trial.finished_at = utcnow()

    late = await persist_tick_to_database(
        events=[], checkpoint_values={**usage, "cost_usd": 9.0}, **args
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        checks["finished_trial_stops_the_tail_and_keeps_its_cost"] = (
            late.trial_finished and float(trial.cost_usd) == 0.25
        )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
