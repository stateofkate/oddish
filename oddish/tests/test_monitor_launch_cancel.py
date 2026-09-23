"""A launcher cancelled after the sandbox exists but before the hand-off must
not leave that sandbox and its credential live behind the released slot."""

from __future__ import annotations

import asyncio

import pytest

from oddish.workers.queue import monitor_launch as ml


async def _no_span(*_a):
    return None


def _job():
    return type("Job", (), {"id": "wj-1", "attempts": 2, "worker_id": "w-1"})()


@pytest.mark.anyio
async def test_a_cancelled_launch_stops_its_sandbox_and_revokes_its_token(monkeypatch):
    stopped, revoked = [], []

    class _Starter:
        provider = "fake"

        async def stop(self, external_id):
            stopped.append(external_id)

    async def _revoke(job_id, *, attempt):
        revoked.append((job_id, attempt))

    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    monkeypatch.setattr(ml, "_close_sandbox_span_after_failed_launch", _no_span)
    await ml._after_cancelled_launch(job=_job(), trial_id="t-1", prepared=None, starter=_Starter(), external_id="sb-1")
    assert stopped == ["sb-1"] and revoked == [("wj-1", 2)]


@pytest.mark.anyio
async def test_a_cancel_before_any_sandbox_only_revokes(monkeypatch):
    revoked = []

    async def _revoke(job_id, *, attempt):
        revoked.append((job_id, attempt))

    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    await ml._after_cancelled_launch(job=_job(), trial_id="t-1", prepared=None, starter=None, external_id=None)
    assert revoked == [("wj-1", 2)]


@pytest.mark.anyio
async def test_teardown_failures_never_mask_the_cancellation(monkeypatch):
    class _Starter:
        provider = "fake"

        async def stop(self, external_id):
            raise RuntimeError("provider down")

    async def _revoke(job_id, *, attempt):
        raise RuntimeError("db down")

    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    monkeypatch.setattr(ml, "_close_sandbox_span_after_failed_launch", _no_span)
    await ml._after_cancelled_launch(job=_job(), trial_id="t-1", prepared=None, starter=_Starter(), external_id="sb-1")


@pytest.mark.anyio
async def test_cancelling_the_caller_mid_teardown_still_runs_every_step(monkeypatch):
    """A second cancel lands while the sandbox stop is in flight: the span close
    and the revoke still run, then the cancel propagates."""
    stopping, release = asyncio.Event(), asyncio.Event()
    stopped, closed, revoked = [], [], []

    class _Starter:
        provider = "fake"

        async def stop(self, external_id):
            stopping.set()
            await release.wait()
            stopped.append(external_id)

    async def _close(job, trial_id, prepared):
        closed.append(trial_id)

    async def _revoke(job_id, *, attempt):
        revoked.append((job_id, attempt))

    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    monkeypatch.setattr(ml, "_close_sandbox_span_after_failed_launch", _close)
    caller = asyncio.create_task(
        ml._after_cancelled_launch(
            job=_job(), trial_id="t-1", prepared=None, starter=_Starter(), external_id="sb-1"
        )
    )
    await stopping.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()  # and again: each cancel is absorbed until the teardown ends
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert stopped == ["sb-1"] and closed == ["t-1"] and revoked == [("wj-1", 2)]
