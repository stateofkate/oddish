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

    async def _init(name, content_hash="new-hash", org_id=ORG, **options):
        return await tasks.initialize_task_upload(
            name, org_id=org_id, content_hash=content_hash, **options
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


async def _add_version(session, task, version, content_hash):
    row = TaskVersionModel(
        id=f"{task.id}-v{version}",
        task_id=task.id,
        version=version,
        task_path=f"s3://tasks/{task.id}/v{version}/",
        task_s3_key=f"tasks/{task.id}/v{version}/",
        content_hash=content_hash,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_same_files_as_another_tasks_current_version_run_there(
    session, init_upload
):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    # A newer, unselected version must not be what the copy is compared to.
    await _add_version(session, word_counter, 2, "hash-2")

    result = await init_upload("wc-debug", content_hash="hash-1")

    assert result.task_id == word_counter.id
    assert result.name == "word-counter"
    assert result.matched_by == "content_hash"
    assert result.existing_task is True
    assert result.content_unchanged is True
    assert result.version == 1
    assert result.version_id == "word-counter-1a2b3c4d-v1"


@pytest.mark.asyncio
async def test_same_files_as_an_older_version_only_create_a_new_task(
    session, init_upload
):
    word_counter = await _task(session, "word-counter-1a2b3c4d", "word-counter")
    current = await _add_version(session, word_counter, 2, "hash-2")
    word_counter.current_version_id = current.id
    await session.flush()

    result = await init_upload("wc-debug", content_hash="hash-1")

    assert result.existing_task is False
    assert result.matched_by is None
    assert result.task_id.startswith("wc-debug-")


@pytest.mark.asyncio
async def test_content_shared_by_several_tasks_creates_a_new_task(session, init_upload):
    await _task(session, "word-counter-1a2b3c4d", "word-counter")
    await _task(session, "word-counter-copy-5e6f7a8b", "word-counter-copy")

    result = await init_upload("wc-debug", content_hash="hash-1")

    assert result.existing_task is False
    assert result.matched_by is None


@pytest.mark.asyncio
async def test_content_match_ignores_other_orgs_deleted_tasks_and_forced_versions(
    session, init_upload
):
    await _task(session, "foreign-task-56bb4ae5", "foreign-task", org_id=OTHER_ORG)
    deleted = await _task(session, "deleted-task-0c0d0e0f", "deleted-task")
    deleted.deleted_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    await session.flush()
    await _task(session, "live-task-9a8b7c6d", "live-task", content_hash="hash-2")

    for content_hash, options in (
        ("hash-1", {}),
        ("hash-2", {"force_new_version": True}),
    ):
        result = await init_upload("wc-debug", content_hash=content_hash, **options)
        assert result.existing_task is False
        assert result.matched_by is None


@pytest.mark.asyncio
async def test_live_name_wins_over_a_content_match(session, init_upload):
    await _task(session, "word-counter-1a2b3c4d", "word-counter")
    named = await _task(session, "wc-debug-5e6f7a8b", "wc-debug", content_hash="old")

    result = await init_upload("wc-debug", content_hash="hash-1")

    assert result.task_id == named.id
    assert result.matched_by is None
    assert result.content_unchanged is False
    assert result.version == 2


@pytest.mark.asyncio
async def test_added_alias_resolves_uploads_and_reports_each_name(session, init_upload):
    system_io = await _task(session, "system-io-1a2b3c4d", "system-io")
    other = await _task(session, "other-5e6f7a8b", "other")
    deleted = await _task(session, "deleted-0c0d0e0f", "deleted")
    deleted.deleted_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    session.add(_alias(other, "taken"))
    await session.flush()
    aliases = {
        "harbor-lh~tasks~sysio": system_io.id,
        "taken": system_io.id,
        "gone": deleted.id,
    }

    preview = await tasks.add_task_aliases_core(
        session, aliases=aliases, org_id=ORG, dry_run=True
    )
    before = await init_upload("harbor-lh~tasks~sysio")

    applied = await tasks.add_task_aliases_core(
        session, aliases=aliases, org_id=ORG, dry_run=False
    )
    again = await tasks.add_task_aliases_core(
        session, aliases=aliases, org_id=ORG, dry_run=False
    )
    result = await init_upload("harbor-lh~tasks~sysio")

    expected = {
        "created": ["harbor-lh~tasks~sysio"],
        "existing": [],
        "conflicts": ["taken"],
        "missing_tasks": ["gone"],
    }
    assert preview == applied == expected
    assert before.existing_task is False
    assert again["created"] == [] and again["existing"] == ["harbor-lh~tasks~sysio"]
    assert (result.task_id, result.matched_by) == (system_io.id, "alias")


@pytest.mark.asyncio
async def test_list_aliases_matches_the_prefix_literally(session):
    task = await _task(session, "system-io-1a2b3c4d", "system-io")
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    session.add_all(
        [
            _alias(task, "smithy~a_b"),
            _alias(task, "smithy~axb"),
            _alias(task, "smithy~a_old", valid_until=now),
            _alias(task, "smithy~a_other", org_id=OTHER_ORG),
        ]
    )
    await session.flush()

    assert await tasks.list_task_aliases_core(
        session, prefix="smithy~a_", org_id=ORG
    ) == {"smithy~a_b": task.id}
