from __future__ import annotations

from types import SimpleNamespace

import pytest
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    AgentSafetyRefusalError,
    ApiClientError,
    ApiInternalServerError,
    ApiRateLimitError,
    ApiUsageLimitError,
    ModelNotFoundError,
    NonZeroAgentExitCodeError,
    UnknownApiError,
)

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

    modules = {
        "harbor.agents.installed.muse_code": SimpleNamespace(MuseCode=FakeMuse),
        "harbor.agents.installed.grok_build": SimpleNamespace(GrokBuild=FakeGrok),
    }
    real_import = patches.importlib.import_module
    monkeypatch.setattr(
        patches.importlib,
        "import_module",
        lambda name: modules[name] if name in modules else real_import(name),
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

    assert type(muse_error) is AgentAuthenticationError
    assert type(grok_error) is ApiUsageLimitError
    assert grok_error.http_status == 402
