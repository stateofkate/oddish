"""A warm snapshot keeps the task files it built from; a monitor booted from it
uses them only when its spec names exactly those files, and fetches otherwise."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from oddish.mon import env, hooks, runner, task_fetch
from oddish.mon.contract import AttemptSpec, WarmRequest
from oddish.workers.launcher import attempt

VERSION = "0123abcd" * 4
OTHER = "fedcba98" * 4


@pytest.fixture
def kept_root(monkeypatch, tmp_path):
    root = tmp_path / "kept"
    monkeypatch.setenv(task_fetch.KEPT_TASKS_VAR, str(root))
    return root


@pytest.fixture
def fetches(monkeypatch):
    """Stands in for the download: writes a small task, records each call."""
    calls: list[Path] = []

    async def fetch(spec, dest):
        calls.append(Path(dest))
        (dest / "environment").mkdir(parents=True, exist_ok=True)
        (dest / "task.toml").write_text("[environment]\n")
        (dest / "environment" / "Dockerfile").write_text("FROM alpine:3.20\n")
        return dest

    monkeypatch.setattr(task_fetch, "fetch_task", fetch)
    return calls


@pytest.fixture
def prepare(monkeypatch, tmp_path, offline_prepare_task):
    """``prepare_task`` for a spec. From the call on, every new temporary dir
    is ``work``."""
    monitor_env = env.MonitorEnv(api_url="https://api.test", token="job-1.1.secret")

    class _Client:
        pass

    async def prepare(spec: AttemptSpec) -> runner.PreparedTask:
        workdir = tmp_path / "work"
        workdir.mkdir(exist_ok=True)
        monkeypatch.setattr(
            runner.tempfile, "mkdtemp", lambda prefix, dir=None: str(workdir)
        )
        task = await runner.prepare_task(_Client(), spec, monitor_env)
        assert isinstance(task.reporter, hooks.LifecycleReporter)
        return task

    return prepare


async def _warm(version: str | None = VERSION, task_id: str = "task-1") -> Path:
    request = WarmRequest(
        task_archive_url="https://signed/archive",
        task_id=task_id,
        task_files_version=version,
    )
    return await task_fetch.fetch_and_keep(request, Path("/nonexistent/fallback"))


@pytest.mark.asyncio
async def test_the_monitor_uses_the_snapshots_copy_of_the_same_files(
    kept_root, fetches, prepare, make_spec, caplog
):
    kept = await _warm()
    assert kept == kept_root / VERSION / f"task-task-1-{VERSION[:8]}"
    assert (kept / "task.toml").is_file()
    fetches.clear()

    caplog.set_level(logging.INFO, logger=task_fetch.__name__)
    task = await prepare(make_spec(task_files_version=VERSION))

    assert task.task_path == kept
    assert fetches == []  # nothing downloaded
    assert caplog.messages == [
        f"metric=monitor_task_files source=kept task_id=task-1 path={kept}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [OTHER, None])
async def test_other_files_or_no_version_are_fetched(
    tmp_path, kept_root, fetches, prepare, make_spec, caplog, version
):
    await _warm()
    fetches.clear()

    caplog.set_level(logging.INFO, logger=task_fetch.__name__)
    task = await prepare(make_spec(task_files_version=version))

    assert fetches == [tmp_path / "work"]
    assert task.task_path == tmp_path / "work"
    assert not task.task_path.is_relative_to(kept_root)
    assert caplog.messages == [
        f"metric=monitor_task_files source=fetched task_id=task-1 path={task.task_path}"
    ]


@pytest.mark.asyncio
async def test_another_tasks_copy_is_never_used(
    tmp_path, kept_root, fetches, prepare, make_spec
):
    await _warm(task_id="task-2")
    fetches.clear()
    task = await prepare(make_spec(task_files_version=VERSION))
    assert fetches == [tmp_path / "work"] and task.task_path == tmp_path / "work"


@pytest.mark.asyncio
async def test_an_interrupted_fetch_keeps_nothing(monkeypatch, kept_root, make_spec):
    async def fail(spec, dest):
        (dest / "environment").mkdir(parents=True)
        raise OSError("connection reset")

    monkeypatch.setattr(task_fetch, "fetch_task", fail)
    with pytest.raises(OSError):
        await _warm()
    # The version directory appears only after a complete fetch.
    assert task_fetch.kept_task(make_spec(task_files_version=VERSION)) is None
    assert not (kept_root / VERSION).exists()


@pytest.mark.asyncio
async def test_a_request_without_a_version_fetches_where_it_did_before(
    tmp_path, kept_root, fetches
):
    request = WarmRequest(task_archive_url="https://signed/archive", task_id="t1")
    assert (
        await task_fetch.fetch_and_keep(request, tmp_path / "task") == tmp_path / "task"
    )
    assert not kept_root.exists()


@pytest.mark.parametrize(
    "task_id, version",
    [
        ("t1", None),
        (None, VERSION),
        ("../t1", VERSION),
        ("a/b", VERSION),
        ("t1", "../" + VERSION[3:]),
        ("t1", VERSION.upper()),
        ("t1", VERSION + "0"),
    ],
)
def test_no_path_is_named_from_a_missing_or_unsafe_part(kept_root, task_id, version):
    assert task_fetch.kept_task_path(task_id, version) is None


def test_a_refused_task_id_is_logged_with_its_reason(kept_root, caplog):
    """Real ids can hold a space or a '+': such a task is fetched every time,
    and the log says so."""
    assert task_fetch.kept_task_path("my task+1", None) is None  # nothing kept anyway
    assert caplog.messages == []
    assert task_fetch.kept_task_path("my task+1", VERSION) is None
    assert caplog.messages == [
        "no kept copy for task_id='my task+1': not a safe dir name"
    ]


def test_every_version_the_launcher_makes_names_a_kept_copy(kept_root):
    """The launcher cuts the version, the monitor checks its shape: a drift
    between the two would silently fetch every task again."""
    version = attempt.task_files_version("tv-1", "tasks/t1/task.tar.gz", [])
    assert task_fetch.kept_task_path("t1", version) is not None
