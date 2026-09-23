"""presign_task_source lists and slices under a directory-shaped prefix."""

import pytest

from oddish.workers.launcher import attempt as monitor_attempt


class _FakeStorage:
    def __init__(self):
        self.listed: list[str] = []

    async def list_keys(self, prefix):
        self.listed.append(prefix)
        keys = ["tasks/abc/task.toml", "tasks/abc/tests/test.sh", "tasks/abcd/other.txt"]
        return [key for key in keys if key.startswith(prefix)]

    async def get_presigned_urls_batch(self, keys):
        return {key: "url:" + key for key in keys}


@pytest.mark.asyncio
async def test_a_stored_key_without_trailing_slash_is_listed_as_a_directory(monkeypatch):
    storage = _FakeStorage()
    monkeypatch.setattr(monitor_attempt, "get_storage_client", lambda: storage)
    archive_url, files = await monitor_attempt.presign_task_source("tasks/abc", None)
    assert storage.listed == ["tasks/abc/"]
    assert archive_url is None
    assert files == {
        "task.toml": "url:tasks/abc/task.toml",
        "tests/test.sh": "url:tasks/abc/tests/test.sh",
    }
    assert not any(path.startswith("/") for path in files)
