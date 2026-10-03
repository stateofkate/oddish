"""A settle interrupted between the trial write and the outcome write.

The trial reads terminal while its job stays RUNNING. A baseline left like
that holds its scope's gated LLM trials BLOCKED, so the re-run must close the
job without repeating the slow artifact upload, and the reconciler's pass
deadline must not be able to cut a settle in half.
"""

import asyncio
import tarfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TrialModel, TrialStatus, WorkerJobModel, WorkerJobStatus
from oddish.workers.launcher import reaper as monitor_reaper
from oddish.workers.launcher import settle as monitor_settle

ARCHIVE_KEY = "tasks/task-1/trials/task-1-0/.oddish-monitor/job-1-2/archive.tar.gz"


class _Storage:
    def __init__(self):
        self.downloaded: list[str] = []
        self.deleted: list[str] = []

    async def download_file(self, key, path):
        self.downloaded.append(key)
        with tarfile.open(path, "w:gz"):
            pass

    async def upload_trial_results(self, trial_id, job_dir, *, subprefix):
        return f"tasks/task-1/trials/{trial_id}/{subprefix}/"

    async def delete_object(self, key):
        self.deleted.append(key)


def _fakes(
    monkeypatch,
    *,
    cancel_first_outcome_write: bool,
    trial_status: TrialStatus = TrialStatus.RUNNING,
):
    job = SimpleNamespace(
        id="job-1", kind="trial", queue_key="nop_oracle",
        status=WorkerJobStatus.RUNNING, attempts=2, max_attempts=6,
        subject_table="trials", subject_id="task-1-0", payload={},
        org_id="org-1", parent_job_id=None, current_worker_id="w-1",
        current_queue_slot=3, provider=None, external_id=None,
        monitor_result_accepted_at="accepted",
        monitor_result={"attempt": 2, "outcome": None, "archive_key": ARCHIVE_KEY},
    )  # fmt: skip
    trial = SimpleNamespace(
        id="task-1-0", task_id="task-1", experiment_id=None, task_version_id=None,
        agent="nop", model=None, environment="docker", harbor_config=None,
        kind="agent", org_id="org-1", billed_user_id=None, attempts=1,
        status=trial_status,
    )  # fmt: skip
    rows = {WorkerJobModel: job, TrialModel: trial}
    calls = SimpleNamespace(trial_writes=0, outcome_writes=0, gate=[], releases=0)

    @asynccontextmanager
    async def get_session():
        async def get(model, _id):
            return rows.get(model)

        yield SimpleNamespace(get=get)

    async def noop(*_args, **_kwargs):
        return None

    async def settle_trial_attempt(**_kwargs):
        calls.trial_writes += 1
        trial.status = TrialStatus.SUCCESS

    async def outcome_for_settled_trial(_claimed, _outcome):
        return SimpleNamespace(success=trial.status == TrialStatus.SUCCESS)

    async def record_outcome(**_kwargs):
        calls.outcome_writes += 1
        if cancel_first_outcome_write and calls.outcome_writes == 1:
            # The reconciler's deadline lands between the two writes.
            raise asyncio.CancelledError
        job.status = WorkerJobStatus.SUCCESS
        return WorkerJobStatus.SUCCESS

    async def resolve_gate(**kwargs):
        calls.gate.append(kwargs["persisted_status"])

    async def release(*_args, **_kwargs):
        calls.releases += 1

    async def mark_trial_settled(_job_id, _attempt, result):
        job.monitor_result = {**result, monitor_settle.TRIAL_SETTLED_KEY: True}

    storage = _Storage()
    handler = SimpleNamespace(outcome_for_settled_trial=outcome_for_settled_trial)
    monkeypatch.setattr(monitor_settle, "get_session", get_session)
    monkeypatch.setattr(monitor_settle, "get_storage_client", lambda: storage)
    monkeypatch.setattr(monitor_settle, "mirror_to_sauron", noop)
    monkeypatch.setattr(monitor_settle, "settle_trial_attempt", settle_trial_attempt)
    monkeypatch.setattr(
        monitor_settle, "ensure_builtin_handlers_registered", lambda: None
    )
    monkeypatch.setattr(monitor_settle, "get_handler", lambda _kind: handler)
    monkeypatch.setattr(monitor_settle, "record_outcome", record_outcome)
    monkeypatch.setattr(
        monitor_settle, "resolve_baseline_gate_after_outcome", resolve_gate
    )
    monkeypatch.setattr(monitor_settle, "release_after_attempt", release)
    monkeypatch.setattr(monitor_settle, "_mark_trial_settled", mark_trial_settled)
    monkeypatch.setattr(monitor_settle, "_post_success_hook", None)
    return job, trial, storage, calls


@pytest.mark.asyncio
async def test_a_re_run_after_an_interrupted_settle_only_closes_the_job(
    monkeypatch,
):
    job, trial, storage, calls = _fakes(monkeypatch, cancel_first_outcome_write=True)

    with pytest.raises(asyncio.CancelledError):
        await monitor_settle.settle_monitor_result("job-1", attempt=2)
    # The stuck state: trial terminal, job still RUNNING.
    assert trial.status == TrialStatus.SUCCESS
    assert job.status == WorkerJobStatus.RUNNING
    assert storage.downloaded == [ARCHIVE_KEY]

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"

    assert job.status == WorkerJobStatus.SUCCESS
    # No second download, upload, or trial write.
    assert storage.downloaded == [ARCHIVE_KEY]
    assert calls.trial_writes == 1
    # The baseline gate is re-checked now that the job is terminal.
    assert calls.gate == [WorkerJobStatus.SUCCESS]
    assert calls.releases == 1
    # The re-run cannot tell whether the interrupted upload landed.
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_an_uninterrupted_settle_still_uploads_and_writes_the_trial(
    monkeypatch,
):
    job, _trial, storage, calls = _fakes(monkeypatch, cancel_first_outcome_write=False)

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"

    assert job.status == WorkerJobStatus.SUCCESS
    assert storage.downloaded == [ARCHIVE_KEY]
    assert calls.trial_writes == 1
    assert storage.deleted == [ARCHIVE_KEY]


@pytest.mark.asyncio
async def test_a_trial_the_end_event_already_marked_still_gets_its_upload(
    monkeypatch,
):
    # Harbor's END lifecycle event marks the trial SUCCESS before the monitor
    # reports, so a terminal trial alone must not skip the settle's work.
    job, _trial, storage, calls = _fakes(
        monkeypatch,
        cancel_first_outcome_write=False,
        trial_status=TrialStatus.SUCCESS,
    )

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"

    assert storage.downloaded == [ARCHIVE_KEY]
    assert calls.trial_writes == 1
    assert job.status == WorkerJobStatus.SUCCESS


@pytest.mark.asyncio
async def test_the_pass_deadline_does_not_cancel_a_settle_in_progress(monkeypatch):
    finished = asyncio.Event()

    async def slow_settle(_job_id, *, attempt):
        await asyncio.sleep(0.2)
        finished.set()
        return "settled"

    monkeypatch.setattr(monitor_reaper, "settle_monitor_result", slow_settle)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await monitor_reaper._settle_shielded("job-1", 2)
    assert not finished.is_set()
    (task,) = monitor_reaper._shielded_settles

    assert await asyncio.wait_for(task, timeout=2) == "settled"
    assert finished.is_set()
    await asyncio.sleep(0)  # let the done callback drop its reference
    assert not monitor_reaper._shielded_settles


def test_the_pass_budget_ends_before_the_reconciler_deadline():
    assert monitor_reaper.PASS_BUDGET_SECONDS < monitor_reaper.PASS_DEADLINE_SECONDS


@pytest.mark.asyncio
async def test_the_reconciler_drain_waits_for_a_detached_settle(monkeypatch):
    finished = asyncio.Event()

    async def slow_settle(_job_id, *, attempt):
        await asyncio.sleep(0.2)
        finished.set()
        return "settled"

    monkeypatch.setattr(monitor_reaper, "settle_monitor_result", slow_settle)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await monitor_reaper._settle_shielded("job-1", 2)

    # Too short a wait reports the settle as still running.
    assert await monitor_reaper.drain_shielded_settles(timeout_seconds=0.01) == 1
    assert await monitor_reaper.drain_shielded_settles(timeout_seconds=2) == 0
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cleanup_drains_detached_settles_even_when_it_fails(monkeypatch):
    from oddish.workers.queue import cleanup

    drained: list[bool] = []

    async def failing_reconcile(*, stale_after_minutes):
        raise RuntimeError("reap transaction failed")

    async def drain():
        drained.append(True)
        return 0

    monkeypatch.setattr(cleanup, "_reconcile_orphaned_queue_state", failing_reconcile)
    monkeypatch.setattr(cleanup, "drain_shielded_settles", drain)

    with pytest.raises(RuntimeError, match="reap transaction failed"):
        await cleanup.cleanup_orphaned_queue_state()
    assert drained == [True]
