"""A launch that fails after opening the agent-sandbox cost span closes it.

The failure path stops the sandbox, so its span must end there too, the same
way settlement closes it; otherwise billing stays open for a stopped sandbox.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from oddish.costs.modal_cost import SpanResources
from oddish.workers.queue import monitor_costs, monitor_launch

_PREPARED = SimpleNamespace(
    trial_attempt=2, experiment_id="exp-1", org_id="org-1", billed_user_id="u-1",
    task_id="task-1", task_version_id="task-1-v3",
)  # fmt: skip


def _resources() -> SpanResources:
    return SpanResources(
        cpu_request=1.0, cpu_limit=4.0, mem_request_mb=1024, mem_limit_mb=4096,
        gpu_type=None, gpu_count=0, price_multiplier=Decimal(1),
        container_class="sandbox", spec_source="pinned",
        cpu_enforcement_mode="limit", mem_enforcement_mode="request",
    )  # fmt: skip


def _job():
    return type("Job", (), {"id": "wj-1", "attempts": 3, "worker_id": "w-1"})()


def _fake_row(monkeypatch, payload):
    class _Session:
        async def get(self, model, job_id):
            assert job_id == "wj-1"
            return SimpleNamespace(payload=payload)

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    monkeypatch.setattr(monitor_launch, "get_session", _get_session)
    settled = []

    async def _settle(state, outcome):
        settled.append((state, outcome))

    monkeypatch.setattr(monitor_launch, "_settle_compute_costs", _settle)
    return settled


@pytest.mark.asyncio
async def test_a_failed_launch_closes_the_span_it_opened(monkeypatch):
    settled = _fake_row(
        monkeypatch,
        {
            monitor_costs.PAYLOAD_KEY: {
                "provider": "daytona",
                "resources": monitor_costs._dump(_resources()),
                "verifier_resources": None,
            }
        },
    )

    await monitor_launch._close_sandbox_span_after_failed_launch(
        _job(), "t-1", _PREPARED
    )

    assert len(settled) == 1
    state, outcome = settled[0]
    assert outcome is None
    assert state.provider == "daytona"
    assert (state.worker_job_id, state.worker_job_attempt) == ("wj-1", 3)
    assert state.resources == _resources()
    assert state.terminal_at is not None  # the span is closed, not left open


@pytest.mark.asyncio
async def test_nothing_is_closed_when_no_span_was_stored(monkeypatch):
    settled = _fake_row(monkeypatch, {"monitor_docker_image": "x"})

    await monitor_launch._close_sandbox_span_after_failed_launch(
        _job(), "t-1", _PREPARED
    )

    assert settled == []
