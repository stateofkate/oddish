"""An inventory snapshot that never returns must not take the reconciler pass
with it (Modal cancels the sweep at its timeout, and nothing after the snapshot
runs). Past the bound the snapshot is an error and the pass goes on."""

from __future__ import annotations

import asyncio

import pytest
from test_cleanup_ec2_orphans import _patch_unrelated_cleanup_phases

from oddish.workers.queue import cleanup


async def _never_returns():
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_hung_thunder_inventory_is_an_error_and_the_pass_goes_on(monkeypatch):
    lines: list[str] = []
    events: list[str] = []
    monkeypatch.setattr(cleanup.console, "print", lines.append)
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", False)
    monkeypatch.setattr(cleanup.settings, "thunder_enabled", True)
    monkeypatch.setattr(cleanup, "INVENTORY_SNAPSHOT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cleanup, "snapshot_thunder_sandboxes", _never_returns)
    _patch_unrelated_cleanup_phases(monkeypatch, events=events, session=object())

    result = await asyncio.wait_for(cleanup.cleanup_orphaned_queue_state(), timeout=5)

    assert result["thunder_orphan_snapshot_errors"] == 1
    assert any(
        "metric=thunder_orphan_snapshot_error" in line and "TimeoutError" in line
        for line in lines
    )
    # The rest of the sweep ran: the stale-job pass opened and committed its session.
    assert "db_open" in events and "db_commit" in events


@pytest.mark.asyncio
async def test_a_hung_ec2_inventory_is_an_error_and_the_pass_goes_on(monkeypatch):
    lines: list[str] = []
    events: list[str] = []

    class _Backend:
        async def snapshot_managed_instances(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(cleanup.console, "print", lines.append)
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", True)
    monkeypatch.setattr(cleanup.settings, "thunder_enabled", False)
    monkeypatch.setattr(cleanup, "INVENTORY_SNAPSHOT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cleanup, "get_backend", lambda _name: _Backend())
    _patch_unrelated_cleanup_phases(monkeypatch, events=events, session=object())

    result = await asyncio.wait_for(cleanup.cleanup_orphaned_queue_state(), timeout=5)

    assert result["ec2_orphan_snapshot_errors"] == 1
    assert any(
        "metric=ec2_orphan_snapshot_error" in line and "TimeoutError" in line
        for line in lines
    )
    assert "db_open" in events and "db_commit" in events


@pytest.mark.asyncio
async def test_analysis_import_budget_preserves_remaining_work_for_next_pass(
    monkeypatch,
):
    from oddish.workers import analysis_trials

    events = []
    _patch_unrelated_cleanup_phases(monkeypatch, events=events, session=object())
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", False)
    monkeypatch.setattr(cleanup.settings, "thunder_enabled", False)
    monkeypatch.setattr(cleanup, "ANALYSIS_REIMPORT_BUDGET_SECONDS", 0.01)
    remaining = ["first", "slow"]
    blocked = True

    async def candidates(_session):
        return list(remaining)

    async def import_result(trial_id):
        assert "db_commit" in events
        if trial_id == "slow" and blocked:
            await asyncio.Event().wait()
        remaining.remove(trial_id)

    monkeypatch.setattr(cleanup, "_heal_stale_audit_imports", candidates)
    monkeypatch.setattr(analysis_trials, "handle_analysis_trial_settled", import_result)
    first = await cleanup.cleanup_orphaned_queue_state()
    assert first["stale_analysis_imports_healed"] == 1
    assert first["stale_analysis_imports_deferred"] == 1
    assert first["stale_analysis_import_errors"] == 0
    assert remaining == ["slow"]
    blocked = False
    second = await cleanup.cleanup_orphaned_queue_state()
    assert second["stale_analysis_imports_healed"] == 1
    assert second["stale_analysis_imports_deferred"] == 0
    assert remaining == []


@pytest.mark.asyncio
async def test_analysis_import_exception_is_a_failure_not_a_deferral(monkeypatch):
    from unittest.mock import AsyncMock
    from oddish.workers import analysis_trials

    _patch_unrelated_cleanup_phases(monkeypatch, events=[], session=object())
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", False)
    monkeypatch.setattr(cleanup.settings, "thunder_enabled", False)
    monkeypatch.setattr(
        cleanup, "_heal_stale_audit_imports", AsyncMock(return_value=["invalid"])
    )
    monkeypatch.setattr(
        analysis_trials,
        "handle_analysis_trial_settled",
        AsyncMock(side_effect=ValueError("invalid artifact")),
    )
    result = await cleanup.cleanup_orphaned_queue_state()
    assert result["stale_analysis_import_errors"] == 1
    assert result["stale_analysis_imports_deferred"] == 0
