"""Lifecycle events through the worker's hook handler against a real Postgres —
run as a subprocess by ``test_monitor_heartbeat_real_db.py`` (no ``test_``
prefix).

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import _harness  # sets the path and the null pool first
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    WorkerJobModel,
)
from oddish.workers.launcher.hook_events import apply_lifecycle_event
from oddish.workers.launcher.token import MonitorAttempt


async def _trial(trial_id: str) -> TrialModel:
    async with get_session() as session:
        return await session.get(TrialModel, trial_id)


async def _main() -> int:
    await _harness.boot()
    suffix = uuid.uuid4().hex[:10]
    trial_id, job_id = f"tr_{suffix}", f"wj_{suffix}"
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
                current_worker_id="launcher-1",
            )
        )
        session.add(
            WorkerJobModel(
                id=job_id,
                kind=WorkerJobKind.TRIAL,
                queue_key="monitor-proof",
                status=WorkerJobStatus.RUNNING,
                subject_table="trials",
                subject_id=trial_id,
                payload={},
                attempts=1,
                current_worker_id="launcher-1",
                provider="modal",
                external_id="outer-sandbox",
            )
        )

    owner = MonitorAttempt(job_id, 1, "launcher-1", trial_id=trial_id)
    stranger = MonitorAttempt(job_id, 1, "someone-else", trial_id=trial_id)
    checks: dict[str, bool] = {}

    await apply_lifecycle_event(stranger, {"event": "agent-start"})
    checks["other_worker_id_is_ignored"] = (await _trial(trial_id)).harbor_stage is None

    await apply_lifecycle_event(owner, {"event": "agent-start"})
    checks["stage_written"] = (await _trial(trial_id)).harbor_stage == "agent_running"

    await apply_lifecycle_event(
        owner,
        {
            "event": "end",
            "result": {
                "verifier_result": {"rewards": {"reward": 1.0}},
                "exception_info": None,
            },
        },
    )
    trial = await _trial(trial_id)
    checks["end_writes_reward_and_success"] = (
        trial.harbor_stage == "completed"
        and trial.status == TrialStatus.SUCCESS
        and float(trial.reward) == 1.0
        and trial.finished_at is not None
    )
    async with get_session() as session:
        job = await session.get(WorkerJobModel, job_id)
        checks["outer_sandbox_handle_untouched"] = (
            job.provider == "modal" and job.external_id == "outer-sandbox"
        )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
