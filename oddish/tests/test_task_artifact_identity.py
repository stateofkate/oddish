"""Execute selected archive bytes, independent of Harbor's parser and caches."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from oddish.db import storage as storage_mod
from test_task_storage import _make_task_archive


class Body:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        return self.data


class MemoryS3:
    def __init__(self, files):
        self.files = files
        self.gets = []

    def etag(self, key):
        return '"' + hashlib.sha256(self.files[key]).hexdigest() + '"'

    async def head_object(self, *, Key, **kwargs):
        if Key not in self.files:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ETag": self.etag(Key)}

    async def get_object(self, *, Key, IfMatch=None, **kwargs):
        self.gets.append((Key, IfMatch))
        if IfMatch and IfMatch != self.etag(Key):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "GetObject",
            )
        return {"Body": Body(self.files[Key])}

    async def put_object(self, *, Key, Body, **kwargs):
        self.files[Key] = Body

    def get_paginator(self, name):
        return self

    async def paginate(self, *, Prefix, **kwargs):
        yield {
            "Contents": [
                {"Key": key, "ETag": self.etag(key)}
                for key in self.files
                if key.startswith(Prefix)
            ]
        }


def client(monkeypatch, files):
    storage = storage_mod.StorageClient()
    storage._client = MemoryS3(files)
    monkeypatch.setattr(storage, "_ensure_client", AsyncMock())
    return storage


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_prefix", ["tasks/task/v1/", "tasks/task/v2/"])
async def test_selected_archive_survives_parser_changes_and_skips_stale_mount(
    monkeypatch, tmp_path, worker_prefix
):
    prefix = "tasks/task/v1/"
    key = prefix + ".oddish-task.tar.gz"
    storage = client(
        monkeypatch,
        {
            key: _make_task_archive(
                {"instruction.md": "selected", "task.toml": "old schema"}
            )
        },
    )
    pinned = await storage.pin_task_archive(prefix)
    artifact = pinned.artifact
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "instruction.md").write_text("another version")
    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: storage)
    monkeypatch.setattr(storage_mod, "resolve_mounted_task_directory", lambda _: stale)
    monkeypatch.setattr(
        "oddish.cli.api.compute_task_content_hash",
        lambda *args: pytest.fail("Parser used for integrity"),
    )
    path, temporary, _ = await storage_mod.resolve_task_directory(
        "task", task_s3_key=worker_prefix, task_path=str(stale), artifact=artifact
    )
    try:
        assert path != stale
        assert (path / "instruction.md").read_text() == "selected"
        assert storage._client.gets == [(key, artifact["etag"])]
    finally:
        storage_mod._cleanup_temp_directory(temporary)


@pytest.mark.asyncio
async def test_overwritten_archive_is_rejected_without_local_fallback(
    monkeypatch, tmp_path
):
    prefix = "tasks/task/v1/"
    key = prefix + ".oddish-task.tar.gz"
    storage = client(
        monkeypatch, {key: _make_task_archive({"instruction.md": "selected"})}
    )
    pinned = await storage.pin_task_archive(prefix)
    artifact = pinned.artifact
    storage._client.files[key] = _make_task_archive({"instruction.md": "overwritten"})
    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: storage)
    with pytest.raises(ValueError, match="PreconditionFailed"):
        await storage_mod.resolve_task_directory(
            "task", task_s3_key=prefix, task_path=str(tmp_path), artifact=artifact
        )


@pytest.mark.asyncio
async def test_loose_files_become_a_conditional_snapshot_without_changing_contents(
    monkeypatch, tmp_path
):
    prefix = "tasks/old/v1/"
    files = {
        prefix + "instruction.md": b"old instruction",
        prefix + "task.toml": b"old configuration",
        prefix + "tests/test.sh": b"exit 1",
    }
    original = dict(files)
    storage = client(monkeypatch, files)
    pinned = await storage.pin_task_archive(prefix)
    artifact = pinned.artifact
    assert "/.oddish-snapshots/" in artifact["archive_key"]
    await storage.download_task_directory(prefix, tmp_path, artifact=artifact)
    assert {
        prefix + p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    } == original
    assert all(etag for key, etag in storage._client.gets)
    # Subsequent runs resolve the published archive directly.
    snapshot_prefix = artifact["archive_key"].rsplit("/", 1)[0] + "/"
    repinned = await storage.pin_task_archive(snapshot_prefix)
    assert repinned.artifact == artifact
    assert pinned.created_snapshot_key == artifact["archive_key"]
    assert repinned.created_snapshot_key is None


@pytest.mark.asyncio
async def test_multiple_historical_archives_are_ambiguous(monkeypatch):
    storage = client(
        monkeypatch,
        {
            "tasks/old/v1/.oddish-task.tar.gz": b"v1",
            "tasks/old/v2/.oddish-task.tar.gz": b"v2",
        },
    )
    with pytest.raises(ValueError, match="ambiguous"):
        await storage.pin_task_archive("tasks/old/")


@pytest.mark.asyncio
async def test_snapshot_refuses_files_added_during_download(monkeypatch):
    prefix = "tasks/old/v1/"
    storage = client(
        monkeypatch,
        {prefix + "instruction.md": b"prompt", prefix + "task.toml": b"config"},
    )
    original = storage.download_bytes

    async def download(*args, **kwargs):
        data = await original(*args, **kwargs)
        storage._client.files[prefix + "new-file"] = b"new"
        return data

    monkeypatch.setattr(storage, "download_bytes", download)
    with pytest.raises(ValueError, match="changed while creating"):
        await storage.pin_task_archive(prefix)
    assert not any(".oddish-snapshots" in key for key in storage._client.files)


@pytest.mark.asyncio
async def test_unpinned_download_ignores_pin_snapshots(monkeypatch, tmp_path):
    prefix = "tasks/old/v1/"
    files = {prefix + "instruction.md": b"old", prefix + "task.toml": b"config"}
    storage = client(monkeypatch, files)
    await storage.pin_task_archive(prefix)
    files[prefix + "instruction.md"] = b"edited after the pin"
    await storage.download_task_directory(prefix, tmp_path)
    assert (tmp_path / "instruction.md").read_bytes() == b"edited after the pin"
    assert not (tmp_path / ".oddish-snapshots").exists()


def test_fingerprint_framing_cannot_be_forged_by_a_file_body(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (two / "a").write_bytes(b"x")
    (two / "b").write_bytes(b"y")
    mode = (two / "b").stat().st_mode
    # Under NUL-only framing, file "a" holding "x\0b\0<mode>\0y" hashed the same as a + b.
    (one / "a").write_bytes(b"x\0b\0" + str(mode).encode() + b"\0y")
    assert storage_mod.task_directory_sha256(one) != storage_mod.task_directory_sha256(two)
