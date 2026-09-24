"""What reaches a warm-up: where the task is and the name of exactly those
files. Never the trial's own config, a token, a model key or a registry login,
because the snapshot it leaves is booted by every later trial of the task."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TaskModel, TaskVersionModel, TrialModel, WorkerJobModel
from oddish.db.storage import StorageClient
from oddish.mon.contract import NOTHING_TO_WARM, WARM_VAR, WarmRequest
from oddish.workers.launcher import attempt as monitor_attempt
from oddish.workers.launcher.providers.modal import ModalStarter
from oddish.workers.launcher.token import MonitorAttempt

SECRET = "sk-user-secret-value"


@pytest.fixture
def rows(monkeypatch):
    """One trial pinned to a task version, whose own config carries keys."""
    trial = SimpleNamespace(
        id="trial-1",
        task_id="t1",
        task_version_id="tv-1",
        attempts=1,
        kind="agent",
        agent="claude-code",
        model="anthropic/claude",
        org_id="org-1",
        billed_user_id=None,
        harbor_config={
            "agent": {"env": {"ANTHROPIC_API_KEY": SECRET}},
            "environment": {"env": {"TOKEN": SECRET}},
        },
    )
    found = {
        TrialModel: trial,
        TaskModel: SimpleNamespace(name="hello", task_s3_key=None, task_path=None),
        WorkerJobModel: SimpleNamespace(payload={"registry_password": SECRET}),
        TaskVersionModel: SimpleNamespace(task_s3_key="tasks/t1/v1", task_path=None),
    }

    class _Session:
        async def get(self, model, key):
            return found[model]

    @asynccontextmanager
    async def _read_session():
        yield _Session()

    class _Storage:
        async def list_keys(self, prefix):
            return [f"{prefix}{StorageClient.TASK_ARCHIVE_OBJECT_NAME}"]

        async def get_presigned_url(self, key):
            return f"https://signed.test/{key}?sig=1"

    monkeypatch.setattr(monitor_attempt, "get_read_session", _read_session)
    monkeypatch.setattr(monitor_attempt, "get_storage_client", lambda: _Storage())
    return found


@pytest.mark.asyncio
async def test_the_warm_request_carries_no_secret_and_names_the_trials_files(rows):
    source = await monitor_attempt.warm_source_for_trial("trial-1", "job-1")
    request = source.request

    assert SECRET not in request.model_dump_json()
    assert request.task_id == "t1" and request.task_files_version
    assert request.task_archive_url.startswith("https://signed.test/tasks/t1/v1/")

    # The monitor's spec names the same files, so it finds the snapshot's copy.
    spec = await monitor_attempt.build_attempt_spec(
        MonitorAttempt("job-1", 1, "launcher-1", trial_id="trial-1")
    )
    assert spec.task_files_version == request.task_files_version


@pytest.mark.asyncio
async def test_modal_gives_the_warm_up_sandbox_only_the_request(fake_modal):
    fake_modal.exit_code = NOTHING_TO_WARM
    request = WarmRequest(
        task_archive_url="https://signed",
        task_id="t1",
        task_files_version="0123abcd" * 4,
    )
    await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=request
    )

    assert fake_modal.secrets == [{WARM_VAR: request.model_dump_json()}]
