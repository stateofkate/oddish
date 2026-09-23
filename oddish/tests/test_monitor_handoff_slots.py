"""A job handed to the in-sandbox monitor keeps its queue slot on every drain
path, not only the Modal worker function: settlement releases it."""

from __future__ import annotations

import pytest

from oddish.dispatch.backends import inprocess
from oddish.workers.queue import queue_manager


@pytest.fixture
def _no_capacity_lane(monkeypatch):
    monkeypatch.setattr(
        "oddish.runtime.sandbox_lifecycle.capacity_provider_for_execution_lane",
        lambda lane: None,
    )
    monkeypatch.setattr(
        queue_manager, "capacity_provider_for_execution_lane", lambda lane: None
    )


@pytest.mark.anyio
@pytest.mark.parametrize("handed_off", [True, False])
async def test_the_assigned_queue_worker_keeps_the_slot_of_a_handed_off_job(
    monkeypatch, _no_capacity_lane, handed_off
):
    released = []

    async def _limit(queue_key):
        return 1

    async def _acquire(**kwargs):
        return 7

    async def _drain(queue_key, *, worker_id, handoff, **kwargs):
        # The drain reports the handoff through the record the caller owns.
        if handed_off:
            handoff.job_id = "job-1"
        return 1

    async def _release(**kwargs):
        released.append(kwargs)

    monkeypatch.setattr(queue_manager, "load_effective_model_concurrency_limit", _limit)
    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _acquire)
    monkeypatch.setattr(queue_manager, "drain_worker_jobs", _drain)
    monkeypatch.setattr(queue_manager, "release_queue_slot", _release)

    assert await queue_manager.run_assigned_queue_worker("q", worker_id="w-1") == 1
    assert (released == []) is handed_off


@pytest.mark.anyio
@pytest.mark.parametrize("handed_off", [True, False])
async def test_the_in_process_dispatcher_keeps_the_slot_of_a_handed_off_job(
    monkeypatch, _no_capacity_lane, handed_off
):
    released = []

    async def _limit(queue_key):
        return 1

    async def _run_job(queue_key, *, worker_id, handoff, **kwargs):
        if handed_off:
            handoff.job_id = "job-1"
        return True

    async def _acquire(**kwargs):
        return 3

    async def _release(**kwargs):
        released.append(kwargs)

    monkeypatch.setattr(
        "oddish.core.model_concurrency.load_effective_model_concurrency_limit", _limit
    )
    dispatcher = inprocess.InProcessDispatcher(
        run_job=_run_job, acquire_slot=_acquire, release_slot=_release
    )
    await dispatcher._safe_run(
        "q", "w-2", harbor_variant_id="default", execution_lane="default"
    )
    assert (released == []) is handed_off


@pytest.mark.anyio
async def test_the_in_process_dispatcher_keeps_the_slot_when_the_run_raises_after_handoff(
    monkeypatch, _no_capacity_lane
):
    """The handoff is a record, not a return value: a run that fails after the
    launch was recorded still leaves the slot with the sandbox."""
    released = []

    async def _limit(queue_key):
        return 1

    async def _run_job(queue_key, *, worker_id, handoff, **kwargs):
        handoff.job_id = "job-1"
        raise RuntimeError("post-launch bookkeeping failed")

    async def _acquire(**kwargs):
        return 3

    async def _release(**kwargs):
        released.append(kwargs)

    monkeypatch.setattr(
        "oddish.core.model_concurrency.load_effective_model_concurrency_limit", _limit
    )
    dispatcher = inprocess.InProcessDispatcher(
        run_job=_run_job, acquire_slot=_acquire, release_slot=_release
    )
    await dispatcher._safe_run(
        "q", "w-2", harbor_variant_id="default", execution_lane="default"
    )
    assert released == []
