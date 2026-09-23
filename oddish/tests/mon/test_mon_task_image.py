"""A restored image must match the entire build context and honor overrides."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.mon import task_image, warm


@pytest.fixture
def task(tmp_path):
    (tmp_path / "task.toml").write_text("[environment]\n")
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM ubuntu:24.04\nCOPY payload /payload\n")
    (environment / "payload").write_text("version one")
    return tmp_path


def test_context_change_invalidates_image_but_download_location_does_not(task, tmp_path):
    import shutil

    original = task_image.image_name(task)
    other = task.parent / (task.name + "-copy")
    shutil.copytree(task, other)
    assert task_image.image_name(other) == original
    (other / "environment" / "payload").write_text("version two")
    assert task_image.image_name(other) != original


@pytest.mark.parametrize("variant", ["compose", "prebuilt", "kwargs", "symlink"])
def test_nonstandard_builds_are_not_replaced(task, variant):
    if variant == "compose":
        (task / "environment" / "docker-compose.yaml").write_text("services: {}")
    elif variant == "symlink":
        (task / "environment" / "link").symlink_to("payload")
    else:
        value = 'docker_image = "private/task:1"' if variant == "prebuilt" else 'kwargs = {target = "test"}'
        (task / "task.toml").write_text("[environment]\n" + value)
    assert task_image.image_name(task) is None


def docker(monkeypatch, exit_code=0):
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args)

        async def communicate():
            return b"build output", None

        async def wait():
            return exit_code

        return SimpleNamespace(returncode=exit_code, communicate=communicate, wait=wait)

    monkeypatch.setattr(task_image.asyncio, "create_subprocess_exec", execute)
    return calls


@pytest.mark.asyncio
async def test_build_and_restore_use_same_local_image_without_pulling(task, monkeypatch):
    calls = docker(monkeypatch)
    assert await task_image.build(task)
    config = {"environment": {"force_build": False}, "timeout_multiplier": 2}
    result = await task_image.reuse(task, config)
    assert result == {**config, "docker_image": task_image.image_name(task)}
    assert "docker_image" not in config
    assert calls == [
        ("docker", "build", "--tag", task_image.image_name(task), str(task / "environment")),
        ("docker", "image", "inspect", task_image.image_name(task)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [
    {"environment": {"force_build": True}},
    {"environment": {"kwargs": {"extra_docker_compose": ["custom.yaml"]}}},
    {"docker_image": "custom/image:v2"},
])
async def test_explicit_build_and_image_contracts_win(task, monkeypatch, config):
    calls = docker(monkeypatch)
    assert await task_image.reuse(task, config) is config
    assert calls == []


@pytest.mark.asyncio
async def test_missing_or_failed_image_uses_original_build_path(task, monkeypatch):
    docker(monkeypatch, exit_code=1)
    assert not await task_image.build(task)
    assert await task_image.reuse(task, None) is None


@pytest.mark.asyncio
async def test_warm_builds_before_snapshot_without_running_task(task, monkeypatch):
    calls = docker(monkeypatch)
    monkeypatch.setenv(warm.WARM_VAR, '{"task_archive_url":"https://signed"}')

    async def fetch(*args):
        return task

    monkeypatch.setattr(warm.task_fetch, "fetch_task", fetch)
    assert await warm.run() == 0
    assert [call[1] for call in calls] == ["build"]


@pytest.mark.parametrize("bad", ["missing", "invalid", "not-a-table"])
def test_a_bad_task_toml_means_no_persisted_image_not_a_crash(task, bad):
    toml = task / "task.toml"
    if bad == "missing":
        toml.unlink()
    elif bad == "invalid":
        toml.write_text("environment = [unclosed")
    else:
        toml.write_text("environment = 1\n")
    assert task_image.image_name(task) is None
