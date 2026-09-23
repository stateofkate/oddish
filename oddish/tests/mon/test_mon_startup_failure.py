"""A monitor that dies before reporting tells the control plane itself."""

from __future__ import annotations

import json

import httpx
import pytest

from oddish.mon import __main__ as mon_main
from oddish.mon import env, startup_failure


def _env():
    return {env.API_URL_VAR: "https://api.example", env.TOKEN_VAR: "job-1.2.secret"}


def _transport(replies):
    """``replies``: per call, an HTTP status (body is a small JSON object)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = replies.pop(0) if len(replies) > 1 else replies[0]
        body = {"detail": "gone"} if status >= 400 else {"accepted": True}
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_reports_a_failed_retryable_result_with_the_log_tails(tmp_path):
    (tmp_path / "mon.log").write_text("x" * 5000 + "\nTraceback: docker refused\n")
    (tmp_path / "dockerd.log").write_text("failed to start containerd\n")
    transport, seen = _transport([200])

    code = await startup_failure.report(
        exit_code=1,
        environ=_env(),
        transport=transport,
        log_paths=(str(tmp_path / "mon.log"), str(tmp_path / "dockerd.log"), "/nope"),
    )

    assert code == 0
    [request] = seen
    assert request.url.path == "/monitor/result"
    assert request.headers["authorization"] == "Bearer job-1.2.secret"
    body = json.loads(request.content)
    assert body["outcome"] is None and body["archive_uploaded"] is False
    assert body["retryable"] is True
    error = body["execution_error"]
    assert error.startswith("Monitor startup failed: oddish-mon exited 1")
    assert "Traceback: docker refused" in error
    assert "failed to start containerd" in error
    assert "/nope" not in error
    assert len(error) < 2 * startup_failure.LOG_TAIL_CHARS + 300


@pytest.mark.asyncio
async def test_an_attempt_that_is_over_is_not_retried():
    transport, seen = _transport([410])
    assert (
        await startup_failure.report(exit_code=3, environ=_env(), transport=transport)
        == 0
    )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_unreachable_control_plane_is_retried_then_dropped():
    transport, seen = _transport([503, 503, 200])
    code = await startup_failure.report(
        exit_code=1, environ=_env(), transport=transport, attempts=5, delay=0
    )
    assert code == 0 and len(seen) == 3

    transport, seen = _transport([503])
    code = await startup_failure.report(
        exit_code=1, environ=_env(), transport=transport, attempts=2, delay=0
    )
    assert code == 0 and len(seen) == 2


@pytest.mark.asyncio
async def test_without_a_credential_there_is_nothing_to_report():
    assert await startup_failure.report(exit_code=1, environ={}) == 0


def test_the_cli_passes_the_exit_code_through(monkeypatch):
    calls = []

    async def _report(*, exit_code):
        calls.append(exit_code)
        return 0

    monkeypatch.setattr(startup_failure, "report", _report)
    monkeypatch.setattr(mon_main, "configure_logging", lambda: None)
    assert mon_main.main(["startup-failed", "5"]) == 0
    assert mon_main.main(["startup-failed"]) == 0
    assert calls == [5, 1]
