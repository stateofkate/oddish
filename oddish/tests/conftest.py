"""Shared pytest fixtures and configuration.

pytest-asyncio gives each test function its own event loop. The SQLAlchemy
asyncpg engine is created at import time and is loop-bound once it opens a
connection, so a second async test that reuses the module-level engine fails
with "Event loop is closed". This autouse fixture recreates the engine before
every test so each test gets a fresh engine bound to its own loop.

The recreated engine uses NullPool (no pooled connections) so recreating it per
test leaks nothing -- connections are opened and closed per use.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types
import warnings
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest_asyncio.fixture
async def session():
    """Async DB session that rolls back after each test, leaving the DB clean."""
    import oddish.db.connection as _conn

    async with _conn.async_session_maker() as s:
        try:
            yield s
        finally:
            await s.rollback()


@pytest.fixture(autouse=True)
def _mon_status_in_tmp(tmp_path, monkeypatch):
    """The monitor records its phase under /run; tests keep it in their tmp dir."""
    import oddish.mon.status as mon_status

    monkeypatch.setattr(mon_status, "STATUS_PATH", tmp_path / "oddish-mon-status.json")


@pytest.fixture(autouse=True)
def _fresh_cost_exclusions():
    """Cost exclusions are cached per process for a minute; tests that insert
    exclusion rows must not see another test's (or their own earlier) snapshot."""
    from oddish.core.cost_exclusions import invalidate_cost_exclusions

    invalidate_cost_exclusions()
    yield
    invalidate_cost_exclusions()


@pytest.fixture(autouse=True)
def _recycle_db_engine():
    """Recreate the shared async engine (NullPool) before each test.

    Runs for every test. For tests that never open a DB connection this is a
    cheap no-op object swap; for async DB tests it guarantees the engine is
    bound to the current test's event loop with no pooled connections to leak.
    """
    import oddish.db.connection as _conn
    from oddish.config import Settings

    Settings.db_use_null_pool = True
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    yield


_DEFAULT_TASK_TOML = """\
version = "1.0"

[task]
name = "abundant/{task_name}"

[metadata]
difficulty = "easy"
description = "a sample task"
reward_type = "binary"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
network_mode = "no-network"
"""


@pytest.fixture
def make_task(tmp_path: Path) -> Callable[..., Path]:
    """Build a minimal, valid Harbor task directory under tmp_path.

    Returns a factory so a single test can build several tasks. Every keyword
    overrides one file; ``None`` omits the file entirely.
    """

    def _make(
        name: str = "sample-task",
        *,
        task_toml: str | None = _DEFAULT_TASK_TOML,
        dockerfile: str | None = "FROM ubuntu:24.04\n",
        test_sh: str | None = "#!/bin/sh\nexit 0\n",
        solve_sh: str | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        task_dir = tmp_path / name
        task_dir.mkdir(parents=True, exist_ok=True)

        if task_toml is not None:
            if task_toml == _DEFAULT_TASK_TOML:
                task_toml = task_toml.replace("{task_name}", name)
            (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")
        (task_dir / "instruction.md").write_text("Solve the task.\n", encoding="utf-8")

        if dockerfile is not None:
            (task_dir / "environment").mkdir(exist_ok=True)
            (task_dir / "environment" / "Dockerfile").write_text(
                dockerfile, encoding="utf-8"
            )
        if test_sh is not None:
            (task_dir / "tests").mkdir(exist_ok=True)
            (task_dir / "tests" / "test.sh").write_text(test_sh, encoding="utf-8")
        if solve_sh is not None:
            (task_dir / "solution").mkdir(exist_ok=True)
            (task_dir / "solution" / "solve.sh").write_text(solve_sh, encoding="utf-8")

        for rel, content in (extra_files or {}).items():
            dest = task_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        return task_dir

    return _make


@pytest.fixture
def docker_kernel_probe(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Stub Harbor's Docker kernel probe and record each call.

    Outside Linux, Harbor's Docker environment probes the host kernel with a
    real ``docker container run``, cached per process, when a task restricts
    its network. Replacing the attribute bypasses that cache; the stub answers
    as Linux does, and monkeypatch restores the real probe after the test.
    """
    from harbor.environments.docker.docker import DockerEnvironment

    calls: list[bool] = []

    def _probe() -> bool:
        calls.append(True)
        return True

    monkeypatch.setattr(
        DockerEnvironment, "_egress_control_kernel_support", staticmethod(_probe)
    )
    return calls


@pytest.fixture
def construct_harbor_trial(
    docker_kernel_probe: list[bool],
) -> Iterator[Callable[..., Awaitable[tuple[Any, Any]]]]:
    """Build Harbor's own Job and its only Trial from a JobConfig, offline.

    Runs Harbor's real trial-config and Trial construction (the Trial builds
    its agent with ``AgentFactory.create_agent_from_config`` and writes its
    lock) and the Trial's result init, which writes the trial's config.json.
    Nothing starts an environment: the Docker kernel probe is stubbed, and a
    subprocess the Docker environment module starts during construction
    fails the test. The log handlers the Job and the Trial attach are closed
    at teardown.
    """
    from harbor.environments.docker import docker as docker_environment

    close_handlers: list[Callable[[], None]] = []
    started: list[Any] = []

    def _tripwire(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        raise RuntimeError("Harbor's Docker environment started a subprocess")

    def _tripwired(module: Any, *names: str) -> Any:
        stand_in = types.SimpleNamespace(**vars(module))
        for name in names:
            setattr(stand_in, name, _tripwire)
        return stand_in

    async def _construct(job_config: Any) -> tuple[Any, Any]:
        from harbor import Job
        from harbor.trial.trial import Trial

        with pytest.MonkeyPatch.context() as patch:
            # Only the Docker environment module's own references change.
            patch.setattr(
                docker_environment, "subprocess", _tripwired(subprocess, "run", "Popen")
            )
            patch.setattr(
                docker_environment,
                "asyncio",
                _tripwired(
                    asyncio, "create_subprocess_exec", "create_subprocess_shell"
                ),
            )
            job = await Job.create(job_config)
            close_handlers.append(job._close_logger_handlers)
            # The configs Job.run submits to its trial queue.
            [trial_config] = job._remaining_trial_configs
            trial = await Trial.create(trial_config)
            close_handlers.append(trial._close_logger_handler)
            with warnings.catch_warnings():
                # Harbor's own result init reads its deprecated Task.checksum.
                warnings.filterwarnings("ignore", "Task.checksum", DeprecationWarning)
                trial._init_result()
        # Harbor swallows some subprocess errors, so check the record too.
        assert not started, f"Harbor's Docker environment ran {started}"
        return job, trial

    yield _construct
    for close in reversed(close_handlers):
        close()
