"""Archil has no GPUs: a GPU trial routed there moves to Modal before launch.

The hosted Archil rollout cannot see a GPU count declared only in task.toml when
the client sends an explicit environment, so the trial handler checks the task
before either launch path reads the trial's environment.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.workers.jobs import handlers


class _Recorder:
    def __init__(self, environment: str | None) -> None:
        self.trial = (
            None if environment is None else SimpleNamespace(environment=environment)
        )
        self.updates: list[str] = []

    def get_session(self):
        recorder = self

        class _Session:
            async def get(self, _model, _id):
                return recorder.trial

            async def execute(self, statement):
                compiled = statement.compile(compile_kwargs={"literal_binds": True})
                recorder.updates.append(str(compiled))

        @asynccontextmanager
        async def _get_session():
            yield _Session()

        return _get_session


def _patch(monkeypatch, recorder: _Recorder, *, requests_gpu) -> list[str]:
    reads: list[str] = []

    async def task_requests_gpu(trial_id: str) -> bool:
        reads.append(trial_id)
        if isinstance(requests_gpu, Exception):
            raise requests_gpu
        return requests_gpu

    monkeypatch.setattr(handlers, "get_session", recorder.get_session())
    monkeypatch.setattr(handlers, "_task_requests_gpu", task_requests_gpu)
    return reads


@pytest.mark.asyncio
async def test_gpu_task_on_archil_moves_to_modal(monkeypatch) -> None:
    recorder = _Recorder("archil")
    _patch(monkeypatch, recorder, requests_gpu=True)

    assert await handlers._move_gpu_trial_off_archil("trial-1") is True

    [update] = recorder.updates
    assert "UPDATE trials SET environment='modal'" in update
    assert "lower(trials.environment) = 'archil'" in update


@pytest.mark.asyncio
async def test_cpu_task_on_archil_stays(monkeypatch) -> None:
    recorder = _Recorder("archil")
    _patch(monkeypatch, recorder, requests_gpu=False)

    assert await handlers._move_gpu_trial_off_archil("trial-1") is False
    assert recorder.updates == []


@pytest.mark.parametrize("environment", ["modal", "ec2", "thunder", None])
@pytest.mark.asyncio
async def test_non_archil_trials_are_not_read(monkeypatch, environment) -> None:
    recorder = _Recorder(environment)
    reads = _patch(monkeypatch, recorder, requests_gpu=True)

    assert await handlers._move_gpu_trial_off_archil("trial-1") is None
    assert reads == []
    assert recorder.updates == []


@pytest.mark.asyncio
async def test_unreadable_task_leaves_the_trial_for_the_launch_path(
    monkeypatch,
) -> None:
    recorder = _Recorder("archil")
    _patch(monkeypatch, recorder, requests_gpu=ValueError("no task"))

    assert await handlers._move_gpu_trial_off_archil("trial-1") is None
    assert recorder.updates == []


@pytest.mark.asyncio
async def test_monitor_route_reuses_the_gpu_answer(monkeypatch) -> None:
    from oddish.config import settings

    reads: list[str] = []

    async def task_requests_gpu(trial_id: str) -> bool:
        reads.append(trial_id)
        return False

    async def no_blocker(_trial_id: str) -> None:
        return None

    monkeypatch.setattr(handlers, "_task_requests_gpu", task_requests_gpu)
    monkeypatch.setattr(handlers, "_monitor_blocker", no_blocker)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", 100)

    assert await handlers._monitor_routes("trial-1", 1, requests_gpu=True) is False
    assert await handlers._monitor_routes("trial-1", 1, requests_gpu=False) is True
    assert reads == []
