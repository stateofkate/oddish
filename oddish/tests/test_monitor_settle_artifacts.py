"""A monitor result whose artifacts were not stored settles the trial exactly as
the worker settles a failed artifact upload: both hand ``_store_trial_results``
an ``artifact_upload_error``. An analysis trial then fails retryably instead of
publishing a SUCCESS nobody can open; any other trial keeps SUCCESS and its
reward, with the loss in ``error_message``.

Two ways settlement ends up without stored artifacts: (a) the monitor reported
``archive_uploaded=False`` (no archive key), (b) settlement's own unpack or
upload to the trial prefix failed."""

import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.db import TrialModel, WorkerJobModel, WorkerJobStatus
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.queue import monitor_settle, trial_handler

ARCHIVE_KEY = "tasks/task-1/trials/task-1-0/.oddish-monitor/job-1-2/archive.tar.gz"
OUTCOME = {"reward": 1.0, "error": None, "exit_code": 0, "duration_sec": 5.0}
# What trial_handler's own upload path reports for the same storage failure.
WORKER_UPLOAD_ERROR = "Failed to upload trial results to S3: TimeoutError: timed out"


class _Storage:
    async def download_file(self, key, path):
        with tarfile.open(path, "w:gz"):
            pass

    async def upload_trial_results(self, trial_id, job_dir, *, subprefix):
        raise TimeoutError("timed out")

    async def delete_object(self, key):
        raise AssertionError("the only copy of the artifacts must be kept")


def _trial(kind: str, attempts: int):
    return SimpleNamespace(
        id="task-1-0", task_id="task-1", experiment_id=None, task_version_id=None,
        agent="oracle", model=None, environment="docker", harbor_config=None,
        kind=kind, org_id="org-1", billed_user_id=None, attempts=attempts,
        max_attempts=3, status=trial_handler.TrialStatus.RUNNING,
        error_message=None, harbor_stage="agent", reward=None,
        harbor_result_path=None, trial_s3_key=None, input_tokens=None,
        cache_tokens=None, output_tokens=None, cost_usd=None, phase_timing=None,
        has_trajectory=False, current_worker_id="w-1", current_queue_slot=None,
        heartbeat_at=None, finished_at=None, superseded_by_trial_id=None,
        deleted_at=None,
    )  # fmt: skip


async def _settle_args(monkeypatch, *, kind, attempts, archive_key):
    """Run settlement with a result that has no stored artifacts; return what it
    handed ``_settle_trial_attempt``."""
    job = SimpleNamespace(
        id="job-1", kind="trial", queue_key="q", status=WorkerJobStatus.RUNNING,
        attempts=2, max_attempts=6, subject_table="trials", subject_id="task-1-0",
        payload={}, org_id="org-1", parent_job_id=None, current_worker_id="w-1",
        current_queue_slot=None, provider=None, external_id=None,
        monitor_result_accepted_at="accepted",
        monitor_result={"attempt": 2, "outcome": OUTCOME, "archive_key": archive_key},
    )  # fmt: skip
    rows = {WorkerJobModel: job, TrialModel: _trial(kind, attempts)}
    captured: dict = {}

    @asynccontextmanager
    async def get_session():
        async def get(model, _id):
            return rows.get(model)

        yield SimpleNamespace(get=get)

    async def noop(*_args, **_kwargs):
        return None

    async def capture(**kwargs):
        captured.update(kwargs)

    async def outcome_for_settled_trial(_claimed, _outcome):
        return SimpleNamespace(success=None)

    handler = SimpleNamespace(outcome_for_settled_trial=outcome_for_settled_trial)
    monkeypatch.setattr(monitor_settle, "get_session", get_session)
    monkeypatch.setattr(monitor_settle, "get_storage_client", lambda: _Storage())
    monkeypatch.setattr(monitor_settle, "_mirror_to_sauron", noop)
    monkeypatch.setattr(monitor_settle, "_settle_trial_attempt", capture)
    monkeypatch.setattr(
        monitor_settle, "ensure_builtin_handlers_registered", lambda: None
    )
    monkeypatch.setattr(monitor_settle, "get_handler", lambda _kind: handler)
    monkeypatch.setattr(monitor_settle, "_record_outcome", noop)
    monkeypatch.setattr(monitor_settle.monitor_token, "revoke_monitor_credential", noop)
    monkeypatch.setattr(monitor_settle, "_post_success_hook", None)

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"
    return captured


async def _stored_state(monkeypatch, trial, **store_kwargs):
    """The trial row after the real ``_store_trial_results``."""

    @asynccontextmanager
    async def trial_session(_trial_id, *, allow_missing=False, with_for_update=False):
        yield SimpleNamespace(get=lambda *_a: None), trial

    async def no_qa_stage(_session, _trial_id):
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    monkeypatch.setattr(queue_module, "maybe_start_qa_stage", no_qa_stage)
    await trial_handler._store_trial_results(
        trial_id=trial.id, trial_attempt=trial.attempts, **store_kwargs
    )
    return trial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "archive_key", [None, ARCHIVE_KEY], ids=["upload_refused", "settle_upload_failed"]
)
@pytest.mark.parametrize(
    ("kind", "attempts", "expected", "reward"),
    [
        ("summarize", 1, trial_handler.TrialStatus.RETRYING, None),
        ("summarize", 3, trial_handler.TrialStatus.FAILED, None),
        ("agent", 1, trial_handler.TrialStatus.SUCCESS, 1.0),
    ],
)
async def test_unstored_artifacts_settle_as_the_worker_settles_a_failed_upload(
    monkeypatch, archive_key, kind, attempts, expected, reward
):
    args = await _settle_args(
        monkeypatch, kind=kind, attempts=attempts, archive_key=archive_key
    )
    assert args["trial_s3_key"] is None
    assert args["artifact_upload_error"].startswith(
        "Failed to upload trial results to S3: "
    )

    via_settle = await _stored_state(
        monkeypatch,
        _trial(kind, attempts),
        outcome=args["execution"].outcome,
        trial_s3_key=args["trial_s3_key"],
        execution_error=args["execution"].execution_error,
        execution_retryable=args["execution"].retryable,
        artifact_upload_error=args["artifact_upload_error"],
    )
    via_worker = await _stored_state(
        monkeypatch,
        _trial(kind, attempts),
        outcome=HarborOutcome(
            **OUTCOME, job_dir=Path("/tmp/job"), job_result_path=None
        ),
        trial_s3_key=None,
        execution_error=None,
        artifact_upload_error=WORKER_UPLOAD_ERROR,
    )

    assert via_settle.status == via_worker.status == expected
    assert via_settle.reward == via_worker.reward == reward
    assert (via_settle.finished_at is None) is (via_worker.finished_at is None)
    assert via_settle.trial_s3_key is via_worker.trial_s3_key is None
    # Visible on the row, never a silent success, and worded as the worker's.
    assert args["artifact_upload_error"] in via_settle.error_message
    assert via_settle.error_message.replace(
        args["artifact_upload_error"], "<upload error>"
    ) == via_worker.error_message.replace(WORKER_UPLOAD_ERROR, "<upload error>")
