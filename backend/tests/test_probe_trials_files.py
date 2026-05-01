"""Unit tests for build_probe_trials_archive (A3: GET /tasks/{id}/probe-trials/files)."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services.trial_archive import build_probe_trials_archive


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self.objects.keys() if k.startswith(prefix)]

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


def _make_task_dir(tmp_path: Path) -> Path:
    d = tmp_path / "test-task"
    d.mkdir()
    (d / "task.toml").write_text("name = 'test-task'\n")
    (d / "instruction.md").write_text("# go")
    (d / "verifier").mkdir()
    (d / "verifier" / "run.sh").write_text("#!/bin/sh\necho 1\n")
    return d


@pytest.mark.asyncio
async def test_includes_task_source_and_probe_trial_artifacts(tmp_path):
    task_dir = _make_task_dir(tmp_path)
    storage = FakeStorage()
    storage.objects["tasks/task_a/trials/task_a-1/result.json"] = b'{"reward": 0}'
    storage.objects["tasks/task_a/trials/task_a-1/agent/transcript.txt"] = b"hi"

    task = SimpleNamespace(name="test-task", task_path=str(task_dir), id="task_a")
    trial = SimpleNamespace(id="task_a-1")

    body = await build_probe_trials_archive(
        storage, task_id="task_a", task=task, probe_trials=[trial],
    )
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert "task/task.toml" in names
    assert "task/instruction.md" in names
    assert "task/verifier/run.sh" in names
    assert "jobs/task_a-1/result.json" in names
    assert "jobs/task_a-1/agent/transcript.txt" in names


@pytest.mark.asyncio
async def test_no_task_path_still_returns_just_jobs(tmp_path):
    storage = FakeStorage()
    storage.objects["tasks/t/trials/t-1/x.json"] = b"x"

    task = SimpleNamespace(name="n", task_path=None, id="t")
    trial = SimpleNamespace(id="t-1")

    body = await build_probe_trials_archive(storage, task_id="t", task=task, probe_trials=[trial])
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["jobs/t-1/x.json"]


@pytest.mark.asyncio
async def test_multiple_trials_each_under_their_own_dir(tmp_path):
    storage = FakeStorage()
    storage.objects["tasks/t/trials/t-1/result.json"] = b"a"
    storage.objects["tasks/t/trials/t-2/result.json"] = b"b"

    task = SimpleNamespace(name="n", task_path=None, id="t")

    body = await build_probe_trials_archive(
        storage, task_id="t", task=task,
        probe_trials=[SimpleNamespace(id="t-1"), SimpleNamespace(id="t-2")],
    )
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["jobs/t-1/result.json", "jobs/t-2/result.json"]


@pytest.mark.asyncio
async def test_skips_noise_paths_and_suffixes(tmp_path):
    storage = FakeStorage()
    storage.objects["tasks/t/trials/t-1/result.json"] = b"keep"
    storage.objects["tasks/t/trials/t-1/sessions/db.sqlite"] = b"skip"
    storage.objects["tasks/t/trials/t-1/lib.so"] = b"skip"

    task = SimpleNamespace(name="n", task_path=None, id="t")
    trial = SimpleNamespace(id="t-1")

    body = await build_probe_trials_archive(storage, task_id="t", task=task, probe_trials=[trial])
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["jobs/t-1/result.json"]
