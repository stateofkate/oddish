"""The warm-up sandbox is priced as a ``warmup_sandbox`` span of the trial
attempt that triggered it, opened at create and closed however it ends."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from oddish.costs.modal_cost import SpanResources
from oddish.mon.contract import WarmRequest
from oddish.workers.launcher import costs as monitor_costs
from oddish.workers.launcher import image_cache as cache
from oddish.workers.launcher.attempt import WarmSource
from oddish.workers.launcher.providers.base import (
    MonitorSandboxError,
    SandboxProvider,
)
from oddish.workers.launcher.providers.modal import ModalStarter


class _Aio:
    def __init__(self, fn):
        self.aio = fn


class _Observer:
    def __init__(self):
        self.events: list[tuple] = []

    async def started(self, external_id):
        self.events.append(("started", external_id))

    async def ended(self):
        self.events.append(("ended",))


_REQUEST = WarmRequest(task_archive_url="https://signed")


# --- Modal: the span follows the warm-up sandbox ------------------------------


@pytest.mark.asyncio
async def test_modal_warm_up_reports_its_sandbox_start_and_end(fake_modal):
    observer = _Observer()
    image = await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=_REQUEST, observer=observer
    )
    assert image == "im-warm"
    assert observer.events == [("started", "sb-1"), ("ended",)]
    assert fake_modal.terminated == 1


@pytest.mark.asyncio
async def test_a_failed_modal_warm_up_still_ends_its_span(fake_modal):
    fake_modal.exit_code = 1
    observer = _Observer()
    with pytest.raises(MonitorSandboxError):
        await ModalStarter().build_warm_image(
            image="/oddish/mon.Dockerfile", request=_REQUEST, observer=observer
        )
    assert observer.events == [("started", "sb-1"), ("ended",)]


@pytest.mark.asyncio
async def test_a_cancelled_modal_warm_up_still_ends_its_span(fake_modal):
    async def _cancelled(*args):
        raise asyncio.CancelledError

    fake_modal.sandbox.exec = _Aio(_cancelled)
    observer = _Observer()
    with pytest.raises(asyncio.CancelledError):
        await ModalStarter().build_warm_image(
            image="/oddish/mon.Dockerfile", request=_REQUEST, observer=observer
        )
    assert observer.events == [("started", "sb-1"), ("ended",)]
    assert fake_modal.terminated == 1


# --- the cache hands the observer to every warm-up it runs -------------------


@pytest.mark.asyncio
async def test_resolve_gives_each_warm_up_attempt_the_observer(monkeypatch):
    entries: dict = {}

    async def _read(provider, key):
        return entries.get((provider, key))

    async def _write(provider, key, entry):
        entries[(provider, key)] = entry

    monkeypatch.setattr(cache, "_read", _read)
    monkeypatch.setattr(cache, "_write", _write)
    seen = []

    class _Starter:
        provider = SandboxProvider.MODAL

        async def build_warm_image(self, *, image, request, size=None, observer=None):
            seen.append(observer)
            if len(seen) == 1:
                raise MonitorSandboxError("first warm-up failed")
            return "im-warm"

    observer = _Observer()
    resolved = await cache.resolve(
        _Starter(),
        monitor_image="/oddish/mon.Dockerfile",
        warm_source=WarmSource(identity="task|v1|", request=_REQUEST),
        has_registry_login=False,
        observer=observer,
    )
    assert resolved.outcome is cache.ImageOutcome.WARMED
    assert seen == [observer, observer]  # the retry is priced too


# --- the launcher's observer writes through the one recorder -----------------


def _resources() -> SpanResources:
    return SpanResources(
        cpu_request=4.0, cpu_limit=4.0, mem_request_mb=16384, mem_limit_mb=16384,
        gpu_type=None, gpu_count=0, price_multiplier=Decimal(1),
        container_class="sandbox", spec_source="pinned",
    )  # fmt: skip


_PREPARED = SimpleNamespace(
    trial_attempt=2, experiment_id="exp-1", org_id="org-1", billed_user_id="u-1",
)  # fmt: skip


@pytest.mark.asyncio
async def test_the_warm_up_span_opens_and_closes_through_the_recorder(monkeypatch):
    calls = []

    async def _transition(**kwargs):
        calls.append(("open", kwargs))

    async def _close(worker_job_id, worker_job_attempt, **kwargs):
        calls.append(("close", {"job": worker_job_id, "at": worker_job_attempt,
                                **kwargs}))  # fmt: skip

    monkeypatch.setattr(monitor_costs, "transition_agent_sandbox", _transition)
    monkeypatch.setattr(monitor_costs, "close_agent_sandboxes", _close)
    span = monitor_costs.WarmUpSpan(
        job_id="wj-1", job_attempt=3, trial_id="t-1", prepared=_PREPARED,
        provider="daytona", resources=_resources(),
    )  # fmt: skip
    await span.started("dt-123")
    await span.ended()

    (_, opened), (_, closed) = calls
    assert opened["component_role"] == "warmup_sandbox"
    assert (opened["worker_job_id"], opened["worker_job_attempt"]) == ("wj-1", 3)
    assert (opened["trial_id"], opened["attempt"]) == ("t-1", 2)
    assert (opened["experiment_id"], opened["org_id"]) == ("exp-1", "org-1")
    assert opened["billed_user_id"] == "u-1"
    assert (opened["provider"], opened["external_id"]) == ("daytona", "dt-123")
    assert opened["resources"] == _resources()
    assert closed["component_role"] == "warmup_sandbox"
    assert (closed["job"], closed["at"]) == ("wj-1", 3)
    assert closed["finished_at"] >= opened["observed_at"]
