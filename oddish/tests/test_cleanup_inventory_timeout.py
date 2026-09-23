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
