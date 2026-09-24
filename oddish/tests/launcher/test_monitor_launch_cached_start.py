"""A cached snapshot is dropped only when the provider no longer has it or
cannot use it, and is then warmed again, never replaced by a cold start. Any
other start failure is the launch's own failure and leaves the snapshot to the
launches still booting from it."""

from __future__ import annotations

import inspect

import pytest

from oddish.workers.launcher import launch as ml
from oddish.workers.launcher.image_cache import Resolved
from oddish.workers.launcher.providers.base import WarmImageUnusable
from oddish.workers.queue import worker_job_single_job


class _Starter:
    provider = "daytona"

    def __init__(self, exists):
        self.exists = exists

    async def warm_image_exists(self, image):
        return self.exists


_TIMED_OUT = RuntimeError("create timed out")


def _failing_then(monkeypatch, forgotten, error=_TIMED_OUT):
    calls: list[str] = []

    async def _start(starter, job, image, env, size):
        calls.append(image)
        if len(calls) == 1:
            raise error
        return "ext-2"

    async def _forget(starter, key):
        forgotten.append(key)

    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml.monitor_image_cache, "forget", _forget)
    monkeypatch.setattr(ml.settings, "monitor_image", "/oddish/mon.Dockerfile")
    return calls


async def _warmed_again():
    return Resolved("snapshot:s2", "key-1", "warmed")


async def _never_warmed_again():
    raise AssertionError("warmed again")


@pytest.mark.parametrize(
    ("exists", "error"),
    [
        (False, RuntimeError("snapshot not found")),  # gone
        (True, WarmImageUnusable("snapshot sandbox is 2/4096, the task needs 4/8192")),
    ],
)
@pytest.mark.asyncio
async def test_a_gone_or_unusable_snapshot_is_forgotten_and_warmed_again(
    monkeypatch, exists, error
):
    forgotten: list[str] = []
    calls = _failing_then(monkeypatch, forgotten, error)

    ext = await ml._start_from_cache(
        _Starter(exists), object(), "snapshot:s1", "key-1", {}, None,
        rewarm=_warmed_again,
    )  # fmt: skip

    assert ext == "ext-2"
    assert calls == ["snapshot:s1", "snapshot:s2"]  # never the plain image
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
            _Starter(exists), object(), "snapshot:s1", "key-1", {}, None,
            rewarm=_never_warmed_again,
        )  # fmt: skip

    assert calls == ["snapshot:s1"]
    assert forgotten == []


@pytest.mark.asyncio
async def test_without_a_cache_entry_the_failure_is_raised_as_is(monkeypatch):
    forgotten: list[str] = []
    calls = _failing_then(monkeypatch, forgotten)

    with pytest.raises(RuntimeError, match="create timed out"):
        await ml._start_from_cache(
            _Starter(False), object(), "img", None, {}, None,
            rewarm=_never_warmed_again,
        )  # fmt: skip

    assert calls == ["img"] and forgotten == []


def test_a_reclaim_clears_the_hand_off_stamp_with_the_other_attempt_columns():
    source = inspect.getsource(worker_job_single_job)
    reset = source[source.index("monitor_token_hash = NULL") :]
    assert "monitor_handed_off_at = NULL" in reset[: reset.index("payload = ")]
