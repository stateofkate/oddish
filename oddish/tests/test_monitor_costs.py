"""The monitor path opens and closes the same cost spans as the worker path."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from oddish.costs.modal_cost import SpanResources
from oddish.db import WorkerJobStatus
from oddish.workers.queue import monitor_costs


def _resources(**overrides) -> SpanResources:
    base = dict(
        cpu_request=1.0, cpu_limit=4.0, mem_request_mb=1024, mem_limit_mb=16384,
        gpu_type="H100", gpu_count=1, price_multiplier=Decimal(1),
        container_class="sandbox", spec_source="pinned",
        cpu_enforcement_mode="limit", mem_enforcement_mode="request",
    )  # fmt: skip
    return SpanResources(**{**base, **overrides})


_PREPARED = SimpleNamespace(
    trial_attempt=2, experiment_id="exp-1", org_id="org-1", billed_user_id="u-1",
    task_id="task-1", task_version_id="task-1-v3",
)  # fmt: skip


def test_resources_survive_the_job_payload_with_every_distinction():
    original = _resources()
    assert monitor_costs._load(monitor_costs._dump(original)) == original
    assert monitor_costs._load(None) is None


def test_settle_rebuilds_the_state_the_launcher_stored():
    payload = {
        monitor_costs.PAYLOAD_KEY: {
            "provider": "daytona",
            "resources": monitor_costs._dump(_resources()),
            "verifier_resources": monitor_costs._dump(_resources(gpu_count=0)),
        }
    }
    state = monitor_costs.cost_state_for_settle(
        payload=payload, job_id="wj-1", job_attempt=3, trial_id="t-1",
        prepared=_PREPARED,
    )  # fmt: skip
    assert state.provider == "daytona"
    assert (state.worker_job_id, state.worker_job_attempt) == ("wj-1", 3)
    assert state.attempt == 2 and state.task_version_id == "task-1-v3"
    assert state.resources == _resources()
    assert state.verifier_resources is None  # the outer span already prices it
    assert state.terminal_at is not None  # closes the span at settlement


def test_nothing_to_close_when_the_launcher_never_opened_a_span():
    assert (
        monitor_costs.cost_state_for_settle(
            payload={"monitor_docker_image": "x"}, job_id="wj-1", job_attempt=1,
            trial_id="t-1", prepared=_PREPARED,
        )
        is None
    )  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount", [1, 0])
async def test_the_cost_payload_is_one_fenced_write_and_a_lost_row_opens_no_span(
    monkeypatch, rowcount
):
    """One UPDATE fenced on id, RUNNING, this attempt and this worker merges
    the cost payload. Reaped or re-claimed (rowcount 0): no span is opened."""
    statements = []

    class _Session:
        async def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(rowcount=rowcount)

        async def get(self, *a, **k):
            raise AssertionError("no read-modify-write of the payload")

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    spans = []

    async def _transition(**kwargs):
        spans.append(kwargs)

    monkeypatch.setattr(monitor_costs, "get_session", _get_session)
    monkeypatch.setattr(monitor_costs, "transition_agent_sandbox", _transition)

    await monitor_costs.open_sandbox_span(
        job_id="wj-1", job_attempt=3, worker_id="w-1", trial_id="t-1",
        prepared=_PREPARED, provider="daytona", external_id="sb-1",
        resources=_resources(), verifier_resources=None,
    )  # fmt: skip

    assert len(statements) == 1
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE worker_jobs SET payload=(worker_jobs.payload ||")
    for fence in ("id", "status", "attempts", "current_worker_id"):
        assert f"worker_jobs.{fence} = " in sql, sql
    values = list(compiled.params.values())
    assert "wj-1" in values and 3 in values and "w-1" in values
    assert WorkerJobStatus.RUNNING in values or "RUNNING" in values
    [patch] = [v for v in values if isinstance(v, dict)]
    assert patch[monitor_costs.PAYLOAD_KEY]["provider"] == "daytona"
    if rowcount == 1:
        assert [(s["worker_job_id"], s["worker_job_attempt"]) for s in spans] == [
            ("wj-1", 3)
        ]
    else:
        assert spans == []
