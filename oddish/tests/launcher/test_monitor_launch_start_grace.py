"""Before hand-off the launcher watches a new sandbox for a short grace period.

Seen in production 24 Sep: a Modal VM sandbox the provider killed a second
after create was handed off anyway, and only the reaper found it 15-18 min
later. A sandbox that is gone within the grace period now fails the attempt
here; one the provider cannot answer for is handed off as before.
"""

from __future__ import annotations

import asyncio

import pytest
from test_monitor_launch_handoff import _job, _Starter, _stub_launch

from oddish.workers.launcher import launch as ml
from oddish.workers.launcher.providers.base import MonitorSandboxError, SandboxState

_RUNNING = SandboxState(gone=False, detail="running")
_DIED = SandboxState(gone=True, detail="exit code -1")


class _Watched(_Starter):
    """A starter whose sandbox reports ``states`` in turn, then the last one."""

    def __init__(self, *states):
        super().__init__()
        self.states = list(states)
        self.polls = 0

    async def sandbox_state(self, external_id):
        assert external_id == "ext-1"
        self.polls += 1
        state = self.states[min(self.polls, len(self.states)) - 1]
        if isinstance(state, BaseException):
            raise state
        return state


def _grace(monkeypatch, *, seconds=0.2, every=0.01, acks_at=None):
    """A short grace; the monitor checks in at the ``acks_at``-th read."""
    monkeypatch.setattr(ml.settings, "monitor_start_grace_seconds", seconds)
    monkeypatch.setattr(ml.settings, "monitor_start_poll_seconds", every)
    reads: list = []

    async def _acked(job):
        reads.append(job.id)
        return acks_at is not None and len(reads) >= acks_at

    monkeypatch.setattr(ml, "_monitor_acked", _acked)
    return reads


def _line(capsys) -> dict[str, str]:
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("metric=monitor_launch ")
    ]
    assert len(lines) == 1, lines
    return dict(field.split("=", 1) for field in lines[0].split(" ")[1:])


@pytest.mark.asyncio
async def test_a_sandbox_gone_at_the_first_poll_fails_the_attempt(monkeypatch, capsys):
    starter = _Watched(_DIED)
    calls = _stub_launch(monkeypatch, starter)
    _grace(monkeypatch)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert starter.polls == 1
    assert calls.handed_off == []
    # The sandbox is stopped and its span closed, as on any failed launch.
    assert starter.stopped == ["ext-1"] and calls.spans_closed == ["job-1"]
    assert calls.revoked == [("job-1", 1)]
    assert len(calls.settled) == 1
    execution = calls.settled[0]["execution"]
    assert execution.retryable is True
    assert "SandboxDiedAtStart" in execution.execution_error
    assert "exit code -1" in execution.execution_error
    line = _line(capsys)
    assert (line["outcome"], line["error_type"]) == ("failed", "SandboxDiedAtStart")


@pytest.mark.asyncio
async def test_a_sandbox_that_dies_during_the_grace_fails_the_attempt(monkeypatch):
    starter = _Watched(_RUNNING, _RUNNING, _DIED)
    calls = _stub_launch(monkeypatch, starter)
    _grace(monkeypatch)

    assert await ml.launch_monitor_attempt(_job()) is None

    assert starter.polls == 3
    assert calls.handed_off == [] and starter.stopped == ["ext-1"]


@pytest.mark.asyncio
async def test_a_healthy_start_hands_off_as_soon_as_the_monitor_checks_in(
    monkeypatch, capsys
):
    starter = _Watched(_RUNNING)
    calls = _stub_launch(monkeypatch, starter)
    reads = _grace(monkeypatch, seconds=30.0, every=0.01, acks_at=2)

    outcome = await asyncio.wait_for(ml.launch_monitor_attempt(_job()), timeout=5)

    assert outcome is not None and outcome.handoff is not None
    assert len(reads) == 2 and starter.polls == 2  # early: not the 30 s grace
    assert calls.handed_off == ["job-1"] and starter.stopped == []
    assert calls.settled == [] and calls.revoked == []
    assert _line(capsys)["outcome"] == "handed_off"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [MonitorSandboxError("modal unreachable"), TimeoutError()]
)
async def test_a_status_error_hands_off(monkeypatch, error):
    """The provider could not be asked: never fail a healthy launch for it."""
    starter = _Watched(error)
    calls = _stub_launch(monkeypatch, starter)
    _grace(monkeypatch, seconds=30.0)

    outcome = await asyncio.wait_for(ml.launch_monitor_attempt(_job()), timeout=5)

    assert outcome is not None and outcome.handoff is not None
    assert starter.polls == 1
    assert calls.handed_off == ["job-1"] and starter.stopped == []
    assert calls.settled == []


@pytest.mark.asyncio
async def test_a_slow_status_call_is_bounded_by_the_grace(monkeypatch):
    starter = _Watched(_RUNNING)

    async def _hangs(external_id):
        starter.polls += 1
        await asyncio.sleep(60)

    starter.sandbox_state = _hangs
    calls = _stub_launch(monkeypatch, starter)
    _grace(monkeypatch, seconds=0.1)

    outcome = await asyncio.wait_for(ml.launch_monitor_attempt(_job()), timeout=5)

    assert outcome is not None and outcome.handoff is not None
    assert calls.handed_off == ["job-1"]


@pytest.mark.asyncio
async def test_the_grace_expiring_with_the_sandbox_running_hands_off(monkeypatch):
    starter = _Watched(_RUNNING)
    calls = _stub_launch(monkeypatch, starter)
    reads = _grace(monkeypatch, seconds=0.1, every=0.01)

    outcome = await asyncio.wait_for(ml.launch_monitor_attempt(_job()), timeout=5)

    assert outcome is not None and outcome.handoff is not None
    assert starter.polls > 1 and len(reads) > 1  # it kept watching
    assert calls.handed_off == ["job-1"] and starter.stopped == []


@pytest.mark.asyncio
async def test_the_launch_heartbeat_keeps_beating_during_the_grace(monkeypatch):
    beats: list = []

    async def _beat(attempt):
        beats.append(attempt.worker_job_id)
        return True

    starter = _Watched(_RUNNING)
    calls = _stub_launch(monkeypatch, starter, beat=_beat)
    _grace(monkeypatch, seconds=0.15)
    before: list = []

    async def _span(**k):
        before.append(len(beats))

    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _span)

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    assert len(beats) > before[0]  # beats landed after create, in the grace
    assert calls.handed_off == ["job-1"]


@pytest.mark.asyncio
async def test_ownership_lost_during_the_grace_stops_the_sandbox(monkeypatch):
    owned = True

    async def _beat(attempt):
        return owned

    starter = _Watched(_RUNNING)
    calls = _stub_launch(monkeypatch, starter, beat=_beat)
    _grace(monkeypatch, seconds=0.15)

    async def _span(**k):
        nonlocal owned
        owned = False  # a reap and re-claim lands right after create

    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _span)

    assert await ml.launch_monitor_attempt(_job()) is None

    assert calls.handed_off == [] and starter.stopped == ["ext-1"]
    # The token and the trial belong to the newer attempt.
    assert calls.settled == [] and calls.revoked == []


@pytest.mark.asyncio
async def test_a_zero_grace_hands_off_without_watching(monkeypatch):
    starter = _Watched(_DIED)
    calls = _stub_launch(monkeypatch, starter)
    reads = _grace(monkeypatch, seconds=0)

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    assert starter.polls == 0 and reads == []
    assert calls.handed_off == ["job-1"]
