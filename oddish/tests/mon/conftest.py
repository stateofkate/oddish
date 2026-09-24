"""What the monitor's tests share: one attempt spec, and ``prepare_task`` kept
away from the status file, registries, the builder and the image cache."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from oddish.mon import runner
from oddish.mon.contract import AttemptSpec


def _attempt_spec(**overrides) -> AttemptSpec:
    fields = dict(
        trial_id="trial-1",
        worker_job_id="job-1",
        worker_job_attempt=2,
        trial_attempt=1,
        trial_kind="agent",
        task_id="task-1",
        task_name="hello",
        agent="oracle",
        model=None,
        harbor_config=None,
        org_id="org-1",
        billed_user_id=None,
    )
    return AttemptSpec(**{**fields, **overrides})


@pytest.fixture
def make_spec() -> Callable[..., AttemptSpec]:
    """Builds the attempt spec; keyword arguments override its fields."""
    return _attempt_spec


@pytest.fixture
def offline_prepare_task(monkeypatch) -> None:
    """``prepare_task`` writes no status, logs into no registry, picks no
    builder and reuses the task's Harbor config as is. A test that needs one
    of them patches it again."""

    async def reuse(task_path, harbor_config):
        return harbor_config

    monkeypatch.setattr(runner, "write_status", lambda **kwargs: None)
    monkeypatch.setattr(runner.registry_login, "login", lambda auth: [])
    monkeypatch.setattr(runner.builder, "env_for", lambda task_path: {})
    monkeypatch.setattr(runner.task_image, "reuse", reuse)
