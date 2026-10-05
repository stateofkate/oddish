"""Which existing task an uploaded folder name resolves to, on real PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from oddish.core import tasks
from oddish.db import TaskAliasModel, TaskModel, TaskVersionModel

ORG = "org-resolver"
OTHER_ORG = "org-resolver-other"


async def _task(session, task_id, name, *, org_id=ORG, content_hash="hash-1"):
    task = TaskModel(
        id=task_id, name=name, org_id=org_id, user="tester", task_path="s3://t/"
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task_id}-v1",
        task_id=task_id,
        version=1,
        task_path=f"s3://tasks/{task_id}/v1/",
        task_s3_key=f"tasks/{task_id}/v1/",
        content_hash=content_hash,
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.flush()
    return task


def _alias(task, name, *, org_id=ORG, **fields):
    return TaskAliasModel(org_id=org_id, task_id=task.id, name=name, **fields)


class _Storage:
    async def get_presigned_upload_url(self, key, **_kwargs):
        return f"https://storage.example/{key}"


@pytest.fixture
def init_upload(session, monkeypatch):
    @asynccontextmanager
    async def _get_session():
        yield session

    monkeypatch.setattr(tasks, "get_session", _get_session)
    monkeypatch.setattr(tasks, "get_storage_client", lambda: _Storage())

    async def _init(name, content_hash="new-hash", org_id=ORG):
        return await tasks.initialize_task_upload(
            name, org_id=org_id, content_hash=content_hash
        )

    return _init


@pytest.mark.asyncio
async def test_live_name_wins_over_alias_and_task_id(session):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    old = await _task(session, "implement-wc-5e6f7a8b", "implement-wc")
    session.add(_alias(word_counter, "implement-wc"))
    await session.flush()

    assert await tasks._resolve_upload_task(session, "implement-wc", ORG) == (
        old,
        None,
    )


@pytest.mark.asyncio
async def test_live_alias_resolves_to_its_task(session, init_upload):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    session.add(_alias(word_counter, "implement-wc"))
    await session.flush()

    result = await init_upload("implement-wc")

    assert result.task_id == word_counter.id
    assert result.name == "word-counter"
    assert result.existing_task is True
    assert result.matched_by == "alias"
    assert result.version == 2
    assert result.s3_key == "tasks/word-counter-1a2b3c4d/v2/"


@pytest.mark.asyncio
async def test_unchanged_content_under_alias_reuses_the_version(session, init_upload):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    session.add(_alias(word_counter, "implement-wc"))
    await session.flush()

    result = await init_upload("implement-wc", content_hash="hash-1")

    assert result.task_id == word_counter.id
    assert result.content_unchanged is True
    assert result.version == 1
    assert result.matched_by == "alias"


@pytest.mark.asyncio
async def test_ended_retracted_and_other_org_aliases_are_ignored(session):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    session.add_all(
        [
            _alias(word_counter, "ended-name", valid_until=now),
            _alias(word_counter, "retracted-name", retracted_at=now),
            _alias(word_counter, "other-org-name", org_id=OTHER_ORG),
        ]
    )
    await session.flush()

    for name in ("ended-name", "retracted-name", "other-org-name"):
        assert await tasks._resolve_upload_task(session, name, ORG) == (None, None)


@pytest.mark.asyncio
async def test_folder_named_after_task_id_resolves_to_that_task(session, init_upload):
    task = await _task(session, "aarch64-task-56bb4ae5", "aarch64-task")

    result = await init_upload("aarch64-task-56bb4ae5")

    assert result.task_id == task.id
    assert result.name == "aarch64-task"
    assert result.matched_by == "task_id"
    assert result.version == 2


@pytest.mark.asyncio
async def test_task_id_of_another_org_or_deleted_task_creates_a_new_task(
    session, init_upload
):
    await _task(session, "foreign-task-56bb4ae5", "foreign-task", org_id=OTHER_ORG)
    deleted = await _task(session, "deleted-task-0c0d0e0f", "deleted-task")
    deleted.deleted_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    await session.flush()

    for name in ("foreign-task-56bb4ae5", "deleted-task-0c0d0e0f"):
        result = await init_upload(name)
        assert result.existing_task is False
        assert result.matched_by is None
        assert result.task_id.startswith(f"{name}-")
        assert result.version == 1


@pytest.mark.asyncio
async def test_alias_of_a_deleted_task_creates_a_new_task(session, init_upload):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    session.add(_alias(word_counter, "implement-wc"))
    word_counter.deleted_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    await session.flush()

    result = await init_upload("implement-wc")

    assert result.existing_task is False
    assert result.task_id.startswith("implement-wc-")
