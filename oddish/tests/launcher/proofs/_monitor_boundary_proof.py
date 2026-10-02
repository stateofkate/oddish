"""The launcher's boundaries and the status check, against a real Postgres with
a scripted sandbox — run as a subprocess by ``test_monitor_heartbeat_real_db.py``.

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace

import _harness  # sets the path and the null pool first
from oddish.config import settings
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import (
    ExperimentModel,
    SandboxCapacityLeaseModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    WorkerJobModel,
    utcnow,
)
from oddish.mon.env import AGENT_ENV_VAR
from oddish.workers.launcher import launch as monitor_launch
from oddish.workers.launcher.providers import base as monitor_sandbox
from oddish.workers.launcher import settle as monitor_settle
from oddish.workers.launcher import reaper as monitor_status_check
from oddish.workers.queue import byok
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob


class FakeStarter:
    provider = "fake"

    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail_start = False
        self.on_start = None
        self.status_answers: list = []

    def provisioned(self, size):
        return size

    async def start(self, *, name, image, env, size=None, bundle=None):
        if self.fail_start:
            raise monitor_sandbox.MonitorSandboxError("provider refused")
        self.started.append(name)
        self.last_env = dict(env)
        if self.on_start is not None:
            await self.on_start()
        return f"sbx-{name}"

    async def stop(self, external_id):
        self.stopped.append(external_id)

    async def status(self, external_id):
        answer = self.status_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        exit_code, output = answer  # scenarios below read as (exit code, stdout)
        return monitor_sandbox.StatusReply(exit_code=exit_code, output=output)


STARTER = FakeStarter()
monitor_sandbox._STARTERS["fake"] = lambda: STARTER


async def _seed(**claim) -> ClaimedWorkerJob:
    sfx = uuid.uuid4().hex[:10]
    async with get_session() as s:
        s.add(ExperimentModel(id=f"ex_{sfx}", name="b"))
        s.add(TaskModel(id=f"ta_{sfx}", name=f"b-{sfx}", user="b", task_path="/x"))
        await s.flush()
        s.add(
            TrialModel(
                id=f"ta_{sfx}-0",
                name="b-0",
                task_id=f"ta_{sfx}",
                experiment_id=f"ex_{sfx}",
                agent="oracle",
                model="nop_oracle",
                provider="default",
                queue_key="b",
                origin=TrialOrigin.ODDISH,
                status=TrialStatus.QUEUED,
            )
        )
        s.add(
            WorkerJobModel(
                id=f"wj_{sfx}", kind=WorkerJobKind.TRIAL, queue_key="b",
                status=WorkerJobStatus.RUNNING, subject_table="trials",
                subject_id=f"ta_{sfx}-0", payload={}, attempts=1,
                current_worker_id="launcher-1", current_queue_slot=0,
                claimed_at=utcnow(), heartbeat_at=utcnow(),
            )
        )  # fmt: skip
    return ClaimedWorkerJob(
        id=f"wj_{sfx}", kind=WorkerJobKind.TRIAL, queue_key="b",
        subject_table="trials", subject_id=f"ta_{sfx}-0", payload={}, attempts=1,
        max_attempts=6, org_id=None, parent_job_id=None, worker_id="launcher-1",
        queue_slot=0, **claim,
    )  # fmt: skip


async def _rows(job):
    async with get_session() as s:
        return await s.get(WorkerJobModel, job.id), await s.get(
            TrialModel, job.subject_id
        )


async def _main() -> int:
    await _harness.boot()
    settings.monitor_api_url = "http://localhost:1"
    settings.monitor_sandbox_starter = "fake"

    async def _no_task_files(job, prepared, provider):
        # The seeded task has no files to read a task.toml from.
        from oddish.workers.harbor.runner import _unknown_sandbox_resources

        return _unknown_sandbox_resources(), None, {}

    monitor_launch._task_facts = _no_task_files
    checks: dict[str, bool] = {}

    # A. crash before the sandbox exists: settled as a failed attempt, no handoff
    job = await _seed()
    STARTER.fail_start = True
    outcome = await monitor_launch.launch_monitor_attempt(job)
    row, trial = await _rows(job)
    checks["A_start_failure_is_not_a_handoff"] = outcome is None
    checks["A_trial_left_for_retry_with_the_reason"] = trial.status in (
        TrialStatus.RETRYING,
        TrialStatus.FAILED,
    ) and "Monitor launch failed" in (trial.error_message or "")
    checks["A_token_revoked"] = row.monitor_token_revoked_at is not None
    checks["A_nothing_to_stop"] = STARTER.stopped == []
    STARTER.fail_start = False

    # C. sandbox up, the monitor never checks in: the launcher hands off at
    # create and the reconciler owns the rest.
    job = await _seed()
    outcome = await monitor_launch.launch_monitor_attempt(job)
    row, trial = await _rows(job)
    checks["C_created_sandbox_is_handed_off"] = (
        outcome is not None and outcome.handoff is not None
    )
    checks["C_sandbox_left_running"] = row.external_id not in STARTER.stopped
    checks["C_handle_was_recorded"] = row.provider == "fake" and bool(row.external_id)
    # Launcher heartbeats alone must never look like a check-in.
    checks["C_launcher_heartbeat_is_not_a_checkin"] = row.monitor_acked_at is None

    async def reconcile(job_id, answers, **values):
        STARTER.status_answers = list(answers)
        async with get_session() as s:
            r = await s.get(WorkerJobModel, job_id)
            for key, value in values.items():
                setattr(r, key, value)
        verdicts = await monitor_status_check.check_stale_monitor_jobs(
            stale_after_minutes=15
        )
        async with get_session() as s:
            return verdicts, (
                await s.get(WorkerJobModel, job_id)
            ).monitor_status_check_count

    # Fresh after the hand-off: nobody looks yet.
    verdicts, _ = await reconcile(row.id, [])
    checks["C_fresh_handoff_is_not_checked"] = verdicts == {}
    # The launcher's beats stopped at hand-off: the row goes stale, the provider
    # is asked, a live sandbox is kept and one with no monitor status is gone.
    stale = utcnow() - timedelta(minutes=20)
    verdicts, count = await reconcile(row.id, [(0, "{}")], heartbeat_at=stale)
    checks["C_stale_alive_is_kept"] = verdicts == {"alive": 1} and count == 0
    verdicts, count = await reconcile(row.id, [(1, '{"reason": "no status"}')])
    checks["C_stale_without_monitor_is_gone_and_reapable"] = (
        verdicts == {"gone": 1} and count == monitor_status_check.REAPABLE
    )
    # Past the launch deadline it is stopped without asking, however fresh.
    settings.monitor_execution_enabled = True
    settings.monitor_launch_deadline_seconds = 60
    verdicts, count = await reconcile(
        row.id,
        [],
        heartbeat_at=utcnow(),
        claimed_at=utcnow() - timedelta(minutes=5),
        monitor_status_check_count=0,
    )
    checks["C_past_launch_deadline_is_gone_and_stopped"] = (
        verdicts == {"gone": 1}
        and count == monitor_status_check.REAPABLE
        and STARTER.stopped[-1:] == [row.external_id]
    )
    settings.monitor_launch_deadline_seconds = None  # back to the derived default
    async with get_session() as s:  # reaped: out of the later sweeps
        (await s.get(WorkerJobModel, row.id)).status = WorkerJobStatus.FAILED

    # D. a handed-off attempt holding a capacity lease: the monitor's own
    # model key reaches the sandbox, and settlement can close costs and
    # release the lease from the row alone.
    job = await _seed(capacity_provider="ec2", capacity_slot=0)
    async with get_session() as session:
        trial = await session.get(TrialModel, job.subject_id)
        trial.agent = "claude-code"
        trial.model = "anthropic/claude-sonnet-5-5"

    async def _users_key(**_):
        return SimpleNamespace(env={"ANTHROPIC_API_KEY": "sk-user-owned"})

    byok.register_byok_resolver(_users_key)
    outcome = await monitor_launch.launch_monitor_attempt(job)
    byok.clear_byok_resolver()
    row, trial = await _rows(job)
    checks["D_create_hands_off"] = outcome is not None and outcome.handoff is not None
    agent_env = json.loads(STARTER.last_env[AGENT_ENV_VAR])
    checks["D_a_users_own_model_key_reaches_the_sandbox"] = (
        agent_env.get("ANTHROPIC_API_KEY") == "sk-user-owned"
    )
    checks["D_the_launcher_stored_what_settlement_needs_to_close_costs"] = (
        row.payload or {}
    ).get("monitor_cost", {}).get("provider") == "fake"
    checks["D_the_launcher_stored_the_capacity_lease"] = (row.payload or {}).get(
        monitor_settle.CAPACITY_PAYLOAD_KEY
    ) == {"provider": "ec2", "slot": 0}
    checks["D_job_still_running_under_the_launchers_id"] = (
        row.status == WorkerJobStatus.RUNNING and row.current_worker_id == "launcher-1"
    )
    async with get_session() as s:
        s.add(
            SandboxCapacityLeaseModel(
                provider="ec2", slot=0, locked_by="launcher-1",
                worker_job_id=row.id, locked_at=utcnow(),
                locked_until=utcnow() + timedelta(minutes=2),
            )
        )  # fmt: skip
    for _ in range(2):  # a re-run after a crash no-ops
        await monitor_settle.release_after_attempt(
            row.id, 1, queue_key="b", slot=None, worker_id="launcher-1",
            payload=row.payload,
        )  # fmt: skip
    async with get_session() as s:
        lease = await s.get(SandboxCapacityLeaseModel, ("ec2", 0))
        checks["D_settlement_releases_the_capacity_lease"] = (
            lease.locked_by is None and lease.worker_job_id is None
        )

    # Status check verdicts on a stale, handed-off job (job D).
    async def stale_and_check(answers):
        STARTER.status_answers = list(answers)
        async with get_session() as s:
            r = await s.get(WorkerJobModel, row.id)
            r.heartbeat_at = utcnow() - timedelta(minutes=20)
        verdicts = await monitor_status_check.check_stale_monitor_jobs(
            stale_after_minutes=15
        )
        async with get_session() as s:
            return verdicts, (
                await s.get(WorkerJobModel, row.id)
            ).monitor_status_check_count

    verdicts, count = await stale_and_check([(0, "{}")])
    checks["alive_keeps_the_attempt"] = verdicts.get("alive") == 1 and count == 0
    verdicts, count = await stale_and_check([ConnectionError("provider down")])
    checks["provider_error_is_unknown_never_dead"] = (
        verdicts.get("unknown") == 1 and count == 0
    )
    for expected in (1, 2, 3):
        verdicts, count = await stale_and_check([asyncio.TimeoutError()] * 3)
        checks[f"unresponsive_sweep_{expected}"] = (
            verdicts.get("unresponsive") == 1 and count == expected
        )
    verdicts, count = await stale_and_check([(0, "{}")])
    checks["an_answer_resets_the_count"] = count == 0
    verdicts, count = await stale_and_check([(1, "")])
    checks["monitor_exited_is_gone_and_reapable"] = (
        verdicts.get("gone") == 1 and count == monitor_status_check.REAPABLE
    )

    async def _reclaim(job_id):  # the reaper re-queues, another claim bumps it
        async with get_session() as s:
            (await s.get(WorkerJobModel, job_id)).attempts += 1

    # E. the prebuilt image is merged into the payload by one fenced write,
    # and a re-claimed row is left alone.
    async def _image(task_id, version):
        return "registry/task:1"

    job = await _seed()
    async with get_session() as s:
        (await s.get(WorkerJobModel, job.id)).payload = {"keep": 1}
    await monitor_launch._resolve_image(job, "task", 1, _image)
    row, _ = await _rows(job)
    checks["E_image_merged_into_an_owned_row"] = row.payload == {
        "keep": 1,
        "monitor_docker_image": "registry/task:1",
    }
    job = await _seed()
    await _reclaim(job.id)
    await monitor_launch._resolve_image(job, "task", 1, _image)
    row, _ = await _rows(job)
    checks["E_image_not_written_to_a_reclaimed_row"] = row.payload == {}
    async with get_session() as s:
        (await s.get(WorkerJobModel, row.id)).status = WorkerJobStatus.FAILED

    # F. the job is reaped and re-claimed while the launch runs, which then
    # fails: its one ownership read leaves the newer attempt's token and trial.
    # A (same failure, still owned) is the control.
    job = await _seed()

    async def _reclaimed_then_refused():
        await _reclaim(job.id)
        raise monitor_sandbox.MonitorSandboxError("provider refused")

    STARTER.on_start = _reclaimed_then_refused
    outcome = await monitor_launch.launch_monitor_attempt(job)
    STARTER.on_start = None
    row, trial = await _rows(job)
    checks["F_reclaimed_failure_is_not_a_handoff"] = outcome is None
    checks["F_reclaimed_failure_settles_no_trial"] = trial.status not in (
        TrialStatus.RETRYING,
        TrialStatus.FAILED,
    ) and "Monitor launch failed" not in (trial.error_message or "")
    checks["F_reclaimed_failure_revokes_no_token"] = (
        row.monitor_token_revoked_at is None
    )
    checks["F_row_still_the_newer_attempts"] = (
        row.status == WorkerJobStatus.RUNNING and row.attempts == 2
    )
    async with get_session() as s:
        (await s.get(WorkerJobModel, row.id)).status = WorkerJobStatus.FAILED

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
