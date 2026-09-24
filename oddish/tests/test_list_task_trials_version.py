"""list_task_trials_for_task scopes its listing to one task version."""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.sharing.helpers import list_task_trials_for_task  # noqa: E402
from oddish.db import TaskModel, get_session  # noqa: E402
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions with the task.
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


@pytest.mark.asyncio
async def test_list_task_trials_filters_by_version(cleanup_task_ids):
    # An s3:// task_path skips create_task's local upload + timeout validation,
    # so the test needs no real storage and no task.toml on disk.
    submission = TaskSubmission(
        name=f"version-filter-{_RUN}",
        task_path="s3://test-bucket/version-filter-fake-task",
        user="test",
        trials=[TrialSpec(agent="nop", model=None)],
    )
    async with get_session() as session:
        task = await create_task(session, submission)
    cleanup_task_ids.append(task.id)

    async with get_session() as session:
        everything = await list_task_trials_for_task(session, task.id)
        assert everything
        version = everything[0].task_version
        assert version is not None
        scoped = await list_task_trials_for_task(session, task.id, version=version)
        other = await list_task_trials_for_task(session, task.id, version=version + 1)

    assert len(scoped) == len(everything)
    assert other == []
