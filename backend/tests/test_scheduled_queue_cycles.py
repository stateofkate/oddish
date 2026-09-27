"""Cancellation boundaries and reporting for scheduled queue work."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.dispatch.cycle import DispatchPlan
from oddish.workers.queue.slots import LaunchReservation
from oddish.workers.queue.worker_job_dispatcher import DispatchUnit
from worker import functions, scheduled


@pytest.fixture
def reports(monkeypatch):
    values = []

    async def record(component, payload):
        values.append((component, payload.copy()))

    monkeypatch.setattr(scheduled, "record_queue_runtime_status", record)
    monkeypatch.setattr(scheduled, "close_database_connections", AsyncMock())
    monkeypatch.setattr(scheduled, "FINALIZE_RESERVE_SECONDS", 0)
    return values


@pytest.mark.asyncio
async def test_cycle_deadline_unwinds_before_reporting_deferred(reports):
    events = []

    async def work(cycle):
        try:
            await asyncio.Event().wait()
        finally:
            events.append("rolled back")

    await scheduled.run_queue_cycle("test", work, timeout_seconds=0.01)
    assert events == ["rolled back"]
    assert reports[-1][1]["state"] == "deferred"
    assert reports[-1][1]["ok"] is False
    assert reports[-1][1]["deferred_phases"] == ["cycle deadline"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TimeoutError("database"), ValueError("bug"), asyncio.CancelledError()]
)
async def test_dependency_errors_and_external_cancellation_propagate(reports, error):
    async def work(cycle):
        raise error

    with pytest.raises(type(error)):
        await scheduled.run_queue_cycle("test", work, timeout_seconds=1)
    assert reports[-1][1]["state"] == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
    )
    assert reports[-1][1]["ok"] is False


@pytest.mark.asyncio
async def test_phase_timeout_allows_later_phase_to_finish(reports):
    async def work(cycle):
        async with cycle.phase("slow", seconds=0.01):
            await asyncio.Event().wait()
        async with cycle.phase("next", seconds=1):
            cycle.payload["committed"] = 3

    await scheduled.run_queue_cycle("test", work, timeout_seconds=1)
    payload = reports[-1][1]
    assert payload["committed"] == 3
    assert payload["deferred_phases"] == ["slow"]
    assert payload["state"] == "deferred"


@pytest.mark.asyncio
async def test_phase_dependency_timeout_is_a_failure_not_budget_exhaustion(reports):
    async def work(cycle):
        async with cycle.phase("database", seconds=1):
            raise TimeoutError("connection timed out")
        cycle.payload["later_phase_ran"] = True

    await scheduled.run_queue_cycle("test", work, timeout_seconds=1)
    payload = reports[-1][1]
    assert payload["state"] == "failed"
    assert payload["deferred_phases"] == []
    assert payload["later_phase_ran"] is True


@pytest.mark.asyncio
async def test_connection_cleanup_is_bounded(reports, monkeypatch):
    async def hang():
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduled, "close_database_connections", hang)
    monkeypatch.setattr(scheduled, "FINALIZE_TIMEOUT_SECONDS", 0.01)
    await asyncio.wait_for(
        scheduled.run_queue_cycle("test", AsyncMock(), timeout_seconds=1), 0.5
    )
    assert reports[-1][1]["state"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_timeout_revokes_tokens_before_reporting(reports, monkeypatch):
    units = [DispatchUnit("test", "default", "default", False, None)] * 2
    plan = DispatchPlan(
        queue_units=(("test", "default", "default"),),
        queue_keys=("test",),
        queued_by_org_queue={},
        running_by_queue={},
        queued_by_queue={"test": 2},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={"test": 2},
        unit_plan=units,
    )
    leases = [LaunchReservation(unit, i, str(i)) for i, unit in enumerate(units)]
    events = []

    async def spawn(**kwargs):
        events.append("spawn-" + kwargs["reservation_token"])
        if kwargs["reservation_token"] == "1":
            await asyncio.Event().wait()
        return object()

    async def release(tokens):
        events.append(("release", tokens))

    async def status(component, payload):
        events.append("status")
        reports.append((component, payload.copy()))

    monkeypatch.setattr(scheduled, "record_queue_runtime_status", status)
    monkeypatch.setattr(functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(functions, "configure_storage_paths", AsyncMock())
    monkeypatch.setattr(functions, "configured_sandbox_capacity_limit", lambda _: 0)
    monkeypatch.setattr(
        functions, "reserve_queue_launches", AsyncMock(return_value=(plan, leases))
    )
    monkeypatch.setattr(functions, "release_launch_reservations", release)
    monkeypatch.setattr(functions, "record_dispatch_snapshot", lambda **kw: None)
    monkeypatch.setattr(functions, "record_dispatch_cycle", lambda **kw: None)
    monkeypatch.setattr(
        functions,
        "select_job_function",
        lambda *a, **kw: (SimpleNamespace(spawn=SimpleNamespace(aio=spawn)), {}),
    )
    monkeypatch.setattr(functions, "DISPATCHER_TIMEOUT_SECONDS", 0.02)
    await functions.poll_queue.get_raw_f()()
    assert events == ["spawn-0", "spawn-1", ("release", ["0", "1"]), "status"]
    assert reports[-1][1]["state"] == "deferred"
    assert reports[-1][1]["spawned"] == 1
    assert reports[-1][1]["launch_outcome_unknown"] is True


@pytest.mark.asyncio
async def test_reconciler_continues_after_a_failed_phase(reports, monkeypatch):
    monkeypatch.setattr(functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(functions, "configure_storage_paths", AsyncMock())
    monkeypatch.setattr(
        functions,
        "cancel_unapproved_runs",
        AsyncMock(side_effect=RuntimeError("query failed")),
    )
    monkeypatch.setattr(
        functions, "cleanup_stale_queue_slots", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        functions,
        "cleanup_orphaned_queue_state",
        AsyncMock(return_value={"stale_analysis_imports_deferred": 3}),
    )
    monkeypatch.setattr(
        functions, "reap_stale_daytona_sandboxes", AsyncMock(return_value=4)
    )
    monkeypatch.setattr(
        functions, "backfill_experiment_owners", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        functions,
        "backfill_github_id",
        AsyncMock(
            return_value=SimpleNamespace(
                as_dict=lambda: {
                    "github_id_backfill_failed": 0,
                    "github_id_backfill_deferred": 0,
                }
            )
        ),
    )
    monkeypatch.setattr(functions.settings, "dynamic_model_concurrency", False)
    await functions.reconcile_queue_state.get_raw_f()()
    payload = reports[-1][1]
    assert payload["state"] == "failed"
    assert payload["counts"]["daytona_terminal_deleted"] == 4
    assert payload["counts"]["stale_slots_cleared"] == 2
    assert payload["deferred_phases"] == [
        "orphaned_state: stale_analysis_imports_deferred=3"
    ]
    assert "unapproved_org_cleanup" in payload["errors"][0]
