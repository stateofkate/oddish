"""Settlement unpacks and deletes the archive key stored with the result,
and keeps it when the artifact upload failed."""

import tarfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TrialModel, WorkerJobModel, WorkerJobStatus
from oddish.db.storage import StorageClient
from oddish.workers.queue import monitor_settle

STORED_KEY = (
    "tasks/task-1/trials/task-1-0/.oddish-monitor/job-1-2/.oddish-trial-import.tar.gz"
)


class _Storage:
    def __init__(self, upload_fails=False):
        self.downloaded: list[str] = []
        self.deleted: list[str] = []
        self.upload_fails = upload_fails

    async def download_file(self, key, path):
        self.downloaded.append(key)
        with tarfile.open(path, "w:gz"):
            pass

    async def upload_trial_results(self, trial_id, job_dir, *, subprefix):
        if self.upload_fails:
            raise RuntimeError("s3 down")
        return f"tasks/task-1/trials/{trial_id}/{subprefix}/"

    async def delete_object(self, key):
        self.deleted.append(key)

    async def delete_trial_import_archive(self, trial_id):
        self.deleted.append(StorageClient._trial_import_archive_key(trial_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_fails", [False, True])
async def test_settle_deletes_the_stored_archive_key_only_after_the_upload(
    monkeypatch, upload_fails
):
    job = SimpleNamespace(
        id="job-1", kind="trial", queue_key="q", status=WorkerJobStatus.RUNNING,
        attempts=2, max_attempts=6, subject_table="trials", subject_id="task-1-0",
        payload={}, org_id="org-1", parent_job_id=None, current_worker_id="w-1",
        current_queue_slot=None, provider=None, external_id=None,
        monitor_result_accepted_at="accepted",
        monitor_result={"attempt": 2, "outcome": None, "archive_key": STORED_KEY},
    )  # fmt: skip
    trial = SimpleNamespace(
        id="task-1-0", task_id="task-1", experiment_id=None, task_version_id=None,
        agent="oracle", model=None, environment="docker", harbor_config=None,
        kind="agent", org_id="org-1", billed_user_id=None, attempts=1,
    )  # fmt: skip
    rows = {WorkerJobModel: job, TrialModel: trial}

    @asynccontextmanager
    async def get_session():
        async def get(model, _id):
            return rows.get(model)

        yield SimpleNamespace(get=get)

    async def noop(*_args, **_kwargs):
        return None

    async def outcome_for_settled_trial(_claimed, _outcome):
        return SimpleNamespace(success=None)

    storage = _Storage(upload_fails)
    handler = SimpleNamespace(outcome_for_settled_trial=outcome_for_settled_trial)
    monkeypatch.setattr(monitor_settle, "get_session", get_session)
    monkeypatch.setattr(monitor_settle, "get_storage_client", lambda: storage)
    monkeypatch.setattr(monitor_settle, "_mirror_to_sauron", noop)
    monkeypatch.setattr(monitor_settle, "_settle_trial_attempt", noop)
    monkeypatch.setattr(
        monitor_settle, "ensure_builtin_handlers_registered", lambda: None
    )
    monkeypatch.setattr(monitor_settle, "get_handler", lambda _kind: handler)
    monkeypatch.setattr(monitor_settle, "_record_outcome", noop)
    monkeypatch.setattr(monitor_settle.monitor_token, "revoke_monitor_credential", noop)
    monkeypatch.setattr(monitor_settle, "_post_success_hook", None)

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"
    assert storage.downloaded == [STORED_KEY]
    # Never the trial's shared import key, which another attempt may own; and
    # never the archive whose artifacts did not reach their final location.
    assert storage.deleted == ([] if upload_fails else [STORED_KEY])


def test_a_payload_that_carries_local_paths_does_not_collide(tmp_path):
    from oddish.workers.queue.monitor_settle import _outcome_from_payload

    outcome = _outcome_from_payload(
        {
            "job_dir": "/sandbox/job",
            "job_result_path": "/sandbox/job/result.json",
            "reward": 1.0,
            "error": None,
            "exit_code": 0,
            "duration_sec": 1.0,
        },
        tmp_path,
    )
    assert outcome is not None
    assert outcome.job_dir == tmp_path
    assert outcome.job_result_path is None  # no result.json here
