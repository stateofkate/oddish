"""The reconciler bounds the launch itself: a monitor job with no check-in past
the launch deadline is a failed start, however fresh the launcher's heartbeat.
Only while the monitor path is on."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from oddish.config import settings
from oddish.workers.launcher import reaper as check
from oddish.workers.launcher.providers import base as monitor_sandbox


class _Result:
    def __init__(self, rows, first=None):
        self._rows = rows
        self._first = first

    def all(self):
        return self._rows

    def first(self):
        return self._first


class _Session:
    def __init__(self, calls, reread):
        self.calls = calls
        self.reread = reread

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _Result([], first=self.reread)


def _fake_sessions(monkeypatch, calls, reread=(False, False)):
    # reread: (has_result, acked) the deadline path's re-read of the row sees.
    @asynccontextmanager
    async def _get_session():
        yield _Session(calls, reread)

    monkeypatch.setattr(check, "get_session", _get_session)

    async def _no_revoke():
        return None

    monkeypatch.setattr(check.monitor_token, "revoke_credentials_of_finished_jobs", _no_revoke)


@pytest.mark.anyio
async def test_the_launch_deadline_is_bound_only_while_the_monitor_path_is_on(monkeypatch):
    calls: list = []
    _fake_sessions(monkeypatch, calls)
    monkeypatch.setattr(settings, "monitor_launch_deadline_seconds", 2700)

    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    await check.check_stale_monitor_jobs(stale_after_minutes=15)
    sql, params = calls[-1]
    assert "monitor_acked_at IS NULL" in sql
    assert params["launch_deadline"] == 2700

    monkeypatch.setattr(settings, "monitor_execution_enabled", False)
    await check.check_stale_monitor_jobs(stale_after_minutes=15)
    assert calls[-1][1]["launch_deadline"] is None


@pytest.mark.anyio
async def test_a_launch_past_its_deadline_is_gone_and_its_sandbox_stopped(monkeypatch):
    calls: list = []
    _fake_sessions(monkeypatch, calls)
    stopped: list = []

    class _Starter:
        async def stop(self, external_id):
            stopped.append(external_id)

        async def status(self, external_id):  # pragma: no cover - must not be asked
            raise AssertionError("a launch past its deadline is not probed")

    monkeypatch.setattr(monitor_sandbox, "get_starter", lambda provider: _Starter())

    # A handle exists (create returned, the monitor never checked in): stop it.
    verdict = await check._check_one(
        "job-1", 1, "modal", "sb-1", False, False, past_launch_deadline=True
    )
    assert verdict == "gone"
    assert stopped == ["sb-1"]
    assert calls[-1][1]["verdict"] == "gone"
    assert calls[-1][1]["reapable"] == check.REAPABLE

    # No handle (stuck before create): nothing to stop, still gone.
    verdict = await check._check_one(
        "job-2", 1, None, None, False, False, past_launch_deadline=True
    )
    assert verdict == "gone"
    assert stopped == ["sb-1"]


@pytest.mark.anyio
async def test_a_launch_inside_its_deadline_is_probed_not_reaped(monkeypatch):
    calls: list = []
    _fake_sessions(monkeypatch, calls)

    class _Reply:
        alive = True

    class _Starter:
        async def status(self, external_id):
            return _Reply()

        async def stop(self, external_id):  # pragma: no cover
            raise AssertionError("an alive monitor is not stopped")

    monkeypatch.setattr(monitor_sandbox, "get_starter", lambda provider: _Starter())
    verdict = await check._check_one("job-3", 1, "modal", "sb-3", False, False)
    assert verdict == "alive"
    assert calls[-1][1]["verdict"] == "alive"


class _NoStopStarter:
    def __init__(self, alive=True):
        self.alive = alive

    async def status(self, external_id):
        return type("_Reply", (), {"alive": self.alive})()

    async def stop(self, external_id):  # pragma: no cover
        raise AssertionError("must not be stopped")


@pytest.mark.anyio
async def test_a_launch_without_a_handle_is_gone_only_past_its_deadline(monkeypatch):
    calls: list = []
    _fake_sessions(monkeypatch, calls)
    monkeypatch.setattr(monitor_sandbox, "get_starter", lambda p: _NoStopStarter())

    # Token minted, image still resolving: no handle is no verdict.
    verdict = await check._check_one("job-4", 1, None, None, False, False)
    assert verdict == "unknown"
    assert calls[-1][1]["verdict"] == "unknown"

    # Past the launch deadline the same row is gone.
    verdict = await check._check_one(
        "job-4", 1, None, None, False, False, past_launch_deadline=True
    )
    assert verdict == "gone"


@pytest.mark.anyio
async def test_a_check_in_after_the_select_is_probed_not_stopped(monkeypatch):
    calls: list = []
    # The select saw no check-in; the re-read sees one.
    _fake_sessions(monkeypatch, calls, reread=(False, True))
    monkeypatch.setattr(monitor_sandbox, "get_starter", lambda p: _NoStopStarter())

    verdict = await check._check_one(
        "job-5", 1, "modal", "sb-5", False, False, past_launch_deadline=True
    )
    assert verdict == "alive"
    assert calls[-1][1]["verdict"] == "alive"
