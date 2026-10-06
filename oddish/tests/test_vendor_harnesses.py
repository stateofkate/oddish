"""Devin CLI wiring through the vendor-service harness registry.

Cursor's behaviour on the same paths stays covered by its existing tests
(test_scoped_model_env_ambient, test_harbor_runner, test_model_hosts,
test_restricted_network_profile), which this registry must not change.
"""

from __future__ import annotations

import pytest
from harbor.models.trial.config import AgentConfig

from oddish.config import settings
from oddish.core.llm_key_fingerprint import provider_key_var
from oddish.vendor_harnesses import (
    CURSOR_CLI,
    DEVIN,
    vendor_harness_for_agent,
    vendor_model_prefix_error,
)
from oddish.workers.harbor import runner as harbor_runner
from oddish.workers.harbor.model_hosts import outbound_hosts_for_model
from oddish.workers.harbor.restricted_network import (
    RestrictedNetworkProfileError,
    agent_keeps_public_model_identity,
    restricted_network_profile_for_config,
)
from oddish.workers.queue import job_tokens


def test_registry_matches_agent_name_and_import_path() -> None:
    assert vendor_harness_for_agent("devin") is DEVIN
    assert vendor_harness_for_agent(" Cursor-CLI ") is CURSOR_CLI
    assert (
        vendor_harness_for_agent(None, "harbor.agents.installed.devin:Devin") is DEVIN
    )
    assert vendor_harness_for_agent("claude-code") is None


def test_devin_key_is_forwarded_from_the_worker_env() -> None:
    ambient = {
        "DEVIN_API_KEY": "cog_1",
        "CURSOR_API_KEY": "cur-1",
        "DATABASE_URL": "postgres://never",
    }
    assert job_tokens.agent_ambient_env("devin", ambient) == {"DEVIN_API_KEY": "cog_1"}
    assert job_tokens.agent_ambient_env("devin", None) == {}


def test_devin_key_enters_redaction_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVIN_API_KEY", "cog_secret")
    agent_config = AgentConfig(name="devin", model_name="devin/swe-2")
    runtime_env = harbor_runner._resolved_runtime_transport_env(
        {}, agent_config=agent_config
    )
    replacements = harbor_runner._runtime_transport_redactions(runtime_env)
    assert runtime_env.get("DEVIN_API_KEY") == "cog_secret"
    assert replacements["cog_secret"] == "[REDACTED]"


def test_fingerprint_reads_each_vendor_key() -> None:
    assert provider_key_var("devin") == "DEVIN_API_KEY"
    assert provider_key_var("cursor") == "CURSOR_API_KEY"


@pytest.mark.parametrize("model", ["devin/swe-2", "devin/claude-opus-4.7"])
def test_devin_models_keep_their_prefix_and_bucket(model: str) -> None:
    assert settings.normalize_trial_model("devin", model) == model
    assert settings.get_provider_for_trial("devin", model) == "devin"
    assert settings.get_queue_key_for_trial("devin", model) == model


@pytest.mark.parametrize("model", ["swe-2", "claude-opus-4-7", "anthropic/x", None])
def test_devin_rejects_models_without_its_prefix(model: str | None) -> None:
    assert vendor_model_prefix_error("devin", model) is not None
    with pytest.raises(ValueError, match="devin/ prefix"):
        settings.normalize_trial_model("devin", model)


def test_prefix_check_is_devin_only_and_skips_stored_reads() -> None:
    assert vendor_model_prefix_error("cursor-cli", "anthropic/claude-opus-5") is None
    assert vendor_model_prefix_error("claude-code", "claude-opus-4-7") is None
    # Read-side rendering of stored rows never raises.
    assert settings.normalize_trial_model("devin", "swe-2", strict=False) == "swe-2"


def test_devin_model_grants_no_host_until_measured() -> None:
    assert outbound_hosts_for_model("devin/swe-2") == []


def test_devin_restricted_phase_fails_closed_and_keeps_model_identity() -> None:
    config = AgentConfig(name="devin", model_name="devin/swe-2")

    assert agent_keeps_public_model_identity(config)
    with pytest.raises(RestrictedNetworkProfileError, match="not established"):
        restricted_network_profile_for_config(config, resolved_env={})
