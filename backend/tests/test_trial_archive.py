"""Unit tests for build_trial_archive.

Pure unit tests against the service function — no DB, auth, or HTTP machinery.
Follows the same pattern as test_task_definition_route.py (A1).
"""
from __future__ import annotations

import io
import tarfile

import pytest

from api.services.trial_archive import build_trial_archive


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self.objects.keys() if k.startswith(prefix)]

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


@pytest.mark.asyncio
async def test_archives_files_under_prefix():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/result.json"] = b'{"ok":true}'
    storage.objects["tasks/exp_a/trials/trial_2/result.json"] = b'{"ok":false}'
    storage.objects["tasks/other/trials/x/y.txt"] = b"not us"  # different experiment

    body = await build_trial_archive(storage, experiment_id="exp_a")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["trial_1/result.json", "trial_2/result.json"]


@pytest.mark.asyncio
async def test_skips_default_path_parts():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/result.json"] = b"keep"
    storage.objects["tasks/exp_a/trials/trial_1/sessions/db.sqlite"] = b"skip"
    storage.objects["tasks/exp_a/trials/trial_1/skills/s.txt"] = b"skip"

    body = await build_trial_archive(storage, experiment_id="exp_a")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["trial_1/result.json"]


@pytest.mark.asyncio
async def test_skips_default_suffixes():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/data.json"] = b"keep"
    storage.objects["tasks/exp_a/trials/trial_1/db.sqlite"] = b"skip"
    storage.objects["tasks/exp_a/trials/trial_1/lib.so"] = b"skip"

    body = await build_trial_archive(storage, experiment_id="exp_a")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["trial_1/data.json"]


@pytest.mark.asyncio
async def test_empty_experiment_yields_empty_tar():
    storage = FakeStorage()
    body = await build_trial_archive(storage, experiment_id="missing")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        members = list(tar.getmembers())
    assert members == []


@pytest.mark.asyncio
async def test_tar_member_content_is_correct():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/result.json"] = b'{"x":1}'

    body = await build_trial_archive(storage, experiment_id="exp_a")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        member = tar.getmember("trial_1/result.json")
        f = tar.extractfile(member)
        assert f.read() == b'{"x":1}'


@pytest.mark.asyncio
async def test_skips_dotfiles():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/result.json"] = b"keep"
    storage.objects["tasks/exp_a/trials/trial_1/.DS_Store"] = b"skip"

    body = await build_trial_archive(storage, experiment_id="exp_a")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["trial_1/result.json"]


@pytest.mark.asyncio
async def test_body_is_valid_gzip():
    storage = FakeStorage()
    storage.objects["tasks/exp_a/trials/trial_1/result.json"] = b'{"ok":true}'

    body = await build_trial_archive(storage, experiment_id="exp_a")
    # gzip magic bytes
    assert body[:2] == b"\x1f\x8b"
