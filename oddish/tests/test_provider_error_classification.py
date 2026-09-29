from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    AgentSafetyRefusalError,
    ApiClientError,
    ApiError,
    ApiInternalServerError,
    ApiRateLimitError,
    ApiResponseStalledError,
    ApiUsageLimitError,
    ModelNotFoundError,
    NetworkConnectionError,
    NonZeroAgentExitCodeError,
    UnknownApiError,
)
from harbor.models.job.config import RetryConfig

from oddish.core.harbor_artifacts import invalidates_score
from oddish.workers.agents.provider_errors import refine_provider_exec_error
from oddish.workers.harbor import patches


def _result(*, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(return_code=1, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    ("output", "expected", "status"),
    [
        (
            "API error (status 403 Forbidden): team cannot access this endpoint",
            AgentAuthenticationError,
            403,
        ),
        (
            "API error (status 400 Bad Request): billing credit balance exhausted",
            ApiUsageLimitError,
            400,
        ),
        (
            "Provider error: payment required; add credits to continue",
            ApiUsageLimitError,
            None,
        ),
        (
            'provider response {"status_code":"403", "message":"forbidden"}',
            AgentAuthenticationError,
            403,
        ),
        (
            "API error: your team does not have enough credits",
            ApiUsageLimitError,
            None,
        ),
        (
            "run ended: the API key was rejected",
            AgentAuthenticationError,
            None,
        ),
        (
            "API error: status=404; model grok-private does not exist",
            ModelNotFoundError,
            404,
        ),
        ("provider response status code: 429", ApiRateLimitError, 429),
        ("Meta provider HTTP/1.1 503 Service Unavailable", ApiInternalServerError, 503),
    ],
)
def test_refines_provider_status_and_account_failures(output, expected, status):
    error = refine_provider_exec_error(
        NonZeroAgentExitCodeError("command failed", return_code=1),
        _result(stderr=output),
        provider="test",
    )

    assert type(error) is expected
    assert error.http_status == status


def test_billing_language_wins_over_auth_shaped_403():
    error = refine_provider_exec_error(
        UnknownApiError("API error", return_code=1),
        _result(stderr="API error (status 403 Forbidden): billing quota exhausted"),
        provider="xAI",
    )

    assert type(error) is ApiUsageLimitError
    assert error.http_status == 403


@pytest.mark.parametrize(
    "fallback", [ApiClientError("400"), AgentAuthenticationError("403")]
)
def test_billing_refines_muse_status_only_classification(fallback):
    error = refine_provider_exec_error(
        fallback,
        _result(stderr="provider billing credit balance exhausted"),
        provider="Meta",
    )

    assert type(error) is ApiUsageLimitError


def test_request_id_is_preserved():
    error = refine_provider_exec_error(
        NonZeroAgentExitCodeError("command failed", return_code=1),
        _result(stderr="API error: status 403, request_id=req_xai-123"),
        provider="xAI",
    )

    assert isinstance(error, AgentAuthenticationError)
    assert error.request_id == "req_xai-123"


def test_unrelated_nonzero_exit_is_not_promoted():
    fallback = NonZeroAgentExitCodeError("tests failed", return_code=1)
    error = refine_provider_exec_error(
        fallback,
        _result(stderr="integration test expected HTTP 403 but received 200"),
        provider="test",
    )

    assert error is fallback


def test_grok_assistant_text_is_not_provider_evidence():
    fallback = NonZeroAgentExitCodeError("grok failed", return_code=1)
    error = refine_provider_exec_error(
        fallback,
        _result(
            stdout=('{"type":"text","data":"the app should handle billing status 403"}')
        ),
        provider="xAI",
    )

    assert error is fallback


def test_grok_structured_error_event_is_provider_evidence():
    error = refine_provider_exec_error(
        NonZeroAgentExitCodeError("grok failed", return_code=1),
        _result(
            stdout=('{"type":"error","message":"API error (status 403 Forbidden)"}')
        ),
        provider="xAI",
    )

    assert type(error) is AgentAuthenticationError
    assert error.http_status == 403


def _muse_record(payload_type: str, payload: dict) -> str:
    """One ``muse exec --json`` stdout record."""
    return json.dumps(
        {"record_type": "event", "payload_type": payload_type, "payload": payload}
    )


def _muse_task(payload_type: str, **event) -> str:
    return _muse_record(payload_type, {"kind": "task_lifecycle", "event": event})


def _muse_run_failed(reason: str) -> str:
    return _muse_record(
        "run.terminal.failed", {"kind": "run_terminal", "reason": reason}
    )


def _refine_muse(*records: str) -> NonZeroAgentExitCodeError:
    return refine_provider_exec_error(
        NonZeroAgentExitCodeError("muse failed", return_code=1),
        _result(stdout="\n".join(records)),
        provider="Meta",
    )


def test_muse_tool_output_is_not_provider_evidence():
    # The task's own test log, streamed back as tool output, is not the
    # provider rejecting Muse.
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.output",
            task_id="t1",
            chunk="FAILED test_login: invalid token; model gpt-x not found",
        )
    )

    assert type(error) is NonZeroAgentExitCodeError


def test_muse_run_ended_by_a_tool_is_not_provider_evidence():
    reason = "pytest: invalid token; billing quota exceeded"
    error = _refine_muse(
        _muse_task("task.lifecycle.proposed", task_id="t1", task_kind="tool.bash"),
        _muse_task("task.lifecycle.failed", task_id="t1", reason=reason),
        _muse_run_failed(reason),
    )

    assert type(error) is NonZeroAgentExitCodeError


def test_muse_helper_failures_that_did_not_end_the_run_are_ignored():
    # Real Muse runs log skill-reminder helpers failing with a 400 while the
    # run carries on; only the run's own failure reason counts.
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.proposed",
            task_id="t1",
            task_kind="reminder.agent.skill-reminder",
        ),
        _muse_task(
            "task.lifecycle.failed",
            task_id="t1",
            reason="model failed: API error 400: reasoning_effort 'low' is not supported",
        ),
    )

    assert type(error) is NonZeroAgentExitCodeError


def test_muse_run_failure_reason_is_provider_evidence():
    reason = "model failed: API error 400 [request_id=req-1]: billing quota exceeded"
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.proposed", task_id="t1", task_kind="model.meta.response"
        ),
        _muse_task("task.lifecycle.failed", task_id="t1", reason=reason),
        _muse_run_failed(reason),
    )

    assert type(error) is ApiUsageLimitError
    assert error.http_status == 400
    assert error.request_id == "req-1"


# Muse's reason when no model stream sent a first event before its deadline.
_MUSE_STREAM_STALL = (
    "no data is reaching this machine from the model service — likely a local "
    "network issue; check connectivity and retry (3 failed attempts over 6m3s, "
    "all [model_stream_first_event_timeout])"
)


def _muse_stall_run() -> str:
    """The stall as ``muse exec --json`` records it, with every payload field."""
    return _muse_record(
        "run.terminal.failed",
        {
            "command_id": "run-1",
            "kind": "run_terminal",
            "reason": _MUSE_STREAM_STALL,
            "run_stream": {"id": "run-1", "kind": "run"},
            "terminal": "failed",
            "text": "",
        },
    )


def test_muse_stream_stall_is_a_retryable_provider_failure():
    # The CLI gave up on the model stream, so the provider ended the run. The
    # stalled class lets Harbor resume the session in the same sandbox.
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.proposed", task_id="t1", task_kind="model.meta.response"
        ),
        _muse_stall_run(),
    )

    assert type(error) is ApiResponseStalledError
    assert isinstance(error, ApiError)
    assert RetryConfig().should_retry(type(error).__name__)
    assert error.http_status is None


def test_muse_stream_stall_overrides_network_text_in_tool_output():
    # Harbor's text classifier reads the whole session, so a tool's own
    # "Connection refused" can arrive as the fallback. The run's reason wins.
    error = refine_provider_exec_error(
        NetworkConnectionError("curl: (7) Connection refused", return_code=1),
        _result(stdout=_muse_stall_run()),
        provider="Meta",
    )

    assert type(error) is ApiResponseStalledError


def test_muse_stream_stall_words_in_tool_output_are_not_provider_evidence():
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.output",
            task_id="t1",
            chunk=f"server log: {_MUSE_STREAM_STALL}",
        )
    )

    assert type(error) is NonZeroAgentExitCodeError


def test_muse_stream_stall_reason_from_a_tool_is_not_provider_evidence():
    error = _refine_muse(
        _muse_task("task.lifecycle.proposed", task_id="t1", task_kind="tool.bash"),
        _muse_task("task.lifecycle.failed", task_id="t1", reason=_MUSE_STREAM_STALL),
        _muse_run_failed(_MUSE_STREAM_STALL),
    )

    assert type(error) is NonZeroAgentExitCodeError


# Muse's stderr when it cannot reach its service before the first model turn.
_MUSE_CATALOG_TRANSPORT_ERROR = (
    "failed to fetch model catalog: transport error: error sending request for "
    "url (https://api.meta.ai/muse-code/models)\n"
)


def test_muse_transport_failure_on_stderr_is_a_network_failure():
    # No model turn ran, so the verifier's zero grades an untouched workspace.
    error = refine_provider_exec_error(
        NonZeroAgentExitCodeError("muse failed", return_code=1),
        _result(stderr=_MUSE_CATALOG_TRANSPORT_ERROR),
        provider="Meta",
    )

    assert type(error) is NetworkConnectionError
    assert error.return_code == 1
    assert invalidates_score(type(error).__name__)


def test_muse_transport_words_in_tool_output_are_not_provider_evidence():
    error = _refine_muse(
        _muse_task(
            "task.lifecycle.output",
            task_id="t1",
            chunk=f"client log: {_MUSE_CATALOG_TRANSPORT_ERROR}",
        )
    )

    assert type(error) is NonZeroAgentExitCodeError


def test_existing_agent_owned_classification_remains_authoritative():
    fallback = AgentSafetyRefusalError("request blocked", return_code=1)
    error = refine_provider_exec_error(
        fallback,
        _result(stderr="API error status 403: request blocked"),
        provider="test",
    )

    assert error is fallback


def test_override_harbor_patch_wraps_muse_and_grok(monkeypatch):
    class FakeMuse:
        def _classify_exec_error(self, command, result):
            return NonZeroAgentExitCodeError("muse failed", return_code=1)

    class FakeGrok:
        def _classify_exec_error(self, command, result):
            return UnknownApiError("grok API error", return_code=1)

    class FakeTbh:
        def _classify_exec_error(self, command, result):
            return NonZeroAgentExitCodeError("tbh failed", return_code=1)

    _fake_agent_modules(
        monkeypatch,
        {
            "harbor.agents.installed.muse_code": SimpleNamespace(MuseCode=FakeMuse),
            "harbor.agents.installed.grok_build": SimpleNamespace(GrokBuild=FakeGrok),
            "harbor.agents.installed.tbh": SimpleNamespace(Tbh=FakeTbh),
        },
    )

    patches._patch_provider_error_classifiers()
    # A second application must not stack wrappers.
    patches._patch_provider_error_classifiers()

    muse_error = FakeMuse()._classify_exec_error(
        "muse exec", _result(stderr="the API key was rejected")
    )
    grok_error = FakeGrok()._classify_exec_error(
        "grok -p", _result(stderr="API error (status 402 Payment Required)")
    )
    tbh_error = FakeTbh()._classify_exec_error(
        "tbh exec", _result(stderr="the API key was rejected")
    )

    assert type(muse_error) is AgentAuthenticationError
    assert type(grok_error) is ApiUsageLimitError
    assert grok_error.http_status == 402
    assert type(tbh_error) is AgentAuthenticationError


@pytest.mark.parametrize(
    ("module_name", "class_name", "command"),
    [
        ("harbor.agents.installed.muse_code", "MuseCode", "muse exec --json"),
        # tbh is Meta's other Muse CLI and writes the same run records.
        ("harbor.agents.installed.tbh", "Tbh", "tbh exec --json"),
    ],
)
def test_override_harbor_patch_retries_muse_stream_stall(
    monkeypatch, module_name, class_name, command
):
    class FakeAgent:
        def _classify_exec_error(self, command, result):
            return NonZeroAgentExitCodeError("muse failed", return_code=1)

    _fake_agent_modules(
        monkeypatch, {module_name: SimpleNamespace(**{class_name: FakeAgent})}
    )

    patches._patch_provider_error_classifiers()

    error = FakeAgent()._classify_exec_error(command, _result(stdout=_muse_stall_run()))

    assert type(error) is ApiResponseStalledError


def _fake_agent_modules(monkeypatch, modules: dict[str, SimpleNamespace]) -> None:
    """Serve fake Harbor agent modules to the patch; skip the other targets."""

    def import_module(name: str) -> SimpleNamespace:
        if name in modules:
            return modules[name]
        if name.startswith("harbor.agents.installed."):
            # The patch skips a module it cannot import. That keeps the real
            # Harbor classes in this process unpatched.
            raise ImportError(name)
        return real_import(name)

    real_import = patches.importlib.import_module
    monkeypatch.setattr(patches.importlib, "import_module", import_module)
