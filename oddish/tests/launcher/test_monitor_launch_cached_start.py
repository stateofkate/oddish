"""A cached snapshot is dropped only when the provider no longer has it. Any
other start failure is the launch's own failure and leaves the snapshot to the
launches still booting from it."""

from __future__ import annotations

import inspect

import pytest

from oddish.workers.launcher import launch as ml
from oddish.workers.queue import worker_job_single_job


class _Starter:
    provider = "daytona"

    def __init__(self, exists):
        self.exists = exists

    async def warm_image_exists(self, image):
        return self.exists


def _failing_then(monkeypatch, forgotten):
    calls: list[str] = []

    async def _start(starter, job, image, env, size):
        calls.append(image)
        if len(calls) == 1:
            raise RuntimeError("create timed out")
        return "ext-cold"

    async def _forget(starter, key):
        forgotten.append(key)

    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml.monitor_image_cache, "forget", _forget)
    monkeypatch.setattr(ml.settings, "monitor_image", "/oddish/mon.Dockerfile")
    return calls


@pytest.mark.asyncio
async def test_a_snapshot_the_provider_lost_is_forgotten_and_the_start_goes_cold(
    monkeypatch,
):
    forgotten: list[str] = []
    calls = _failing_then(monkeypatch, forgotten)

    ext = await ml._start_from_cache(
        _Starter(False), object(), "snapshot:s1", "key-1", {}, None
    )

    assert ext == "ext-cold"
    assert calls == ["snapshot:s1", "/oddish/mon.Dockerfile"]
    assert forgotten == ["key-1"]


@pytest.mark.parametrize("exists", [True, None])
@pytest.mark.asyncio
async def test_any_other_start_failure_keeps_the_snapshot_and_fails_the_launch(
    monkeypatch, exists
):
    forgotten: list[str] = []
    calls = _failing_then(monkeypatch, forgotten)

    with pytest.raises(RuntimeError, match="create timed out"):
        await ml._start_from_cache(
            _Starter(exists), object(), "snapshot:s1", "key-1", {}, None
        )

    assert calls == ["snapshot:s1"]
    assert forgotten == []


@pytest.mark.asyncio
async def test_without_a_cache_entry_the_failure_is_raised_as_is(monkeypatch):
    forgotten: list[str] = []
    calls = _failing_then(monkeypatch, forgotten)

    with pytest.raises(RuntimeError, match="create timed out"):
        await ml._start_from_cache(_Starter(False), object(), "img", None, {}, None)

    assert calls == ["img"] and forgotten == []


def test_a_reclaim_clears_the_hand_off_stamp_with_the_other_attempt_columns():
    source = inspect.getsource(worker_job_single_job)
    reset = source[source.index("monitor_token_hash = NULL") :]
    assert "monitor_handed_off_at = NULL" in reset[: reset.index("payload = ")]


@pytest.mark.asyncio
async def test_a_snapshot_the_provider_calls_unusable_is_forgotten_even_though_it_exists(
    monkeypatch,
):
    from oddish.workers.launcher.providers import base as monitor_sandbox

    forgotten: list[str] = []
    calls: list[str] = []

    async def _start(starter, job, image, env, size):
        calls.append(image)
        if len(calls) == 1:
            raise monitor_sandbox.WarmImageUnusable(
                "snapshot sandbox is 2/4096, the task needs 4/8192"
            )
        return "ext-cold"

    async def _forget(starter, key):
        forgotten.append(key)

    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml.monitor_image_cache, "forget", _forget)
    monkeypatch.setattr(ml.settings, "monitor_image", "/oddish/mon.Dockerfile")

    ext = await ml._start_from_cache(
        _Starter(True), object(), "snapshot:s1", "key-1", {}, None
    )

    assert ext == "ext-cold"
    assert calls == ["snapshot:s1", "/oddish/mon.Dockerfile"]
    assert forgotten == ["key-1"]
