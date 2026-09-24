"""presign_task_source lists and slices under a directory-shaped prefix, and
names exactly the files it hands out."""

import pytest

from oddish.workers.launcher import attempt as monitor_attempt


class _FakeStorage:
    def __init__(self, keys=None):
        self.listed: list[str] = []
        self.keys = keys or [
            "tasks/abc/task.toml",
            "tasks/abc/tests/test.sh",
            "tasks/abcd/other.txt",
        ]

    async def list_keys(self, prefix):
        self.listed.append(prefix)
        return [key for key in self.keys if key.startswith(prefix)]

    async def get_presigned_url(self, key):
        return "url:" + key

    async def get_presigned_urls_batch(self, keys):
        return {key: "url:" + key for key in keys}


@pytest.mark.asyncio
async def test_a_stored_key_without_trailing_slash_is_listed_as_a_directory(
    monkeypatch,
):
    storage = _FakeStorage()
    monkeypatch.setattr(monitor_attempt, "get_storage_client", lambda: storage)
    archive_url, files, version = await monitor_attempt.presign_task_source(
        "tasks/abc", None
    )
    assert storage.listed == ["tasks/abc/"]
    assert archive_url is None
    assert files == {
        "task.toml": "url:tasks/abc/task.toml",
        "tests/test.sh": "url:tasks/abc/tests/test.sh",
    }
    assert not any(path.startswith("/") for path in files)
    # Not pinned to a task version: nothing names these files, always fetched.
    assert version is None


@pytest.mark.asyncio
async def test_the_version_names_the_pinned_files_and_moves_with_an_overwrite(
    monkeypatch,
):
    name = monitor_attempt.StorageClient.TASK_ARCHIVE_OBJECT_NAME
    storage = _FakeStorage([f"tasks/abc/v3/{name}"])
    monkeypatch.setattr(monitor_attempt, "get_storage_client", lambda: storage)

    first = await monitor_attempt.presign_task_source(
        "tasks/abc/v3", None, task_version_id="tv-3"
    )
    again = await monitor_attempt.presign_task_source(
        "tasks/abc/v3", None, task_version_id="tv-3"
    )
    assert first.archive_url == f"url:tasks/abc/v3/{name}" and first.files == {}
    assert first.version and first.version == again.version  # stable across presigns

    # An in-place overwrite keeps the version id and moves it to a new source.
    storage.keys = [f"tasks/abc/v3-revisions/0123/{name}"]
    overwritten = await monitor_attempt.presign_task_source(
        "tasks/abc/v3-revisions/0123", None, task_version_id="tv-3"
    )
    assert overwritten.version != first.version


def test_the_version_ignores_listing_order_and_needs_a_pinned_version():
    files = ["tasks/abc/task.toml", "tasks/abc/tests/test.sh"]
    version = monitor_attempt.task_files_version("tv-1", None, files)
    assert version == monitor_attempt.task_files_version("tv-1", None, files[::-1])
    assert len(version) == 32 and int(version, 16) >= 0
    assert version != monitor_attempt.task_files_version("tv-2", None, files)
    assert monitor_attempt.task_files_version(None, None, files) is None
    assert monitor_attempt.task_files_version("tv-1", None, []) is None
