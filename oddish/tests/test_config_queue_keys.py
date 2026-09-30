from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import (
    GEOMETRIC_DEFAULT_BASE_URL,
    NOP_ORACLE_QUEUE_KEY,
    Settings,
    infer_model_provider_prefix,
    is_meta_model,
    is_meta_public_model,
    meta_harness_model_id,
    meta_public_bare_model_id,
    normalize_model_id,
    require_geometric_served_model_id,
    resolve_meta_public_api_key,
    to_anthropic_api_model_id,
    to_meta_public_model_id,
)


def _settings(monkeypatch, *, clear_openai_env: bool = True, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_NOP_ORACLE_CONCURRENCY", raising=False)
    if clear_openai_env:
        monkeypatch.delenv("ODDISH_OPENAI_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
        monkeypatch.delenv("ODDISH_AZURE_OPENAI_DEPLOYMENTS", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    return Settings(_env_file=None, **kwargs)


def test_nop_and_oracle_use_single_id_for_model_and_queue(monkeypatch):
    settings = _settings(monkeypatch)

    # The model id and queue key must be the SAME single string so the stored
    # model, the queue key, and the concurrency bucket never drift apart.
    assert settings.normalize_trial_model("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.normalize_trial_model("oracle", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("oracle", None) == NOP_ORACLE_QUEUE_KEY


def test_nop_oracle_variants_force_single_id(monkeypatch):
    settings = _settings(monkeypatch)

    # Suffixed / prefixed baseline variants must be treated like plain
    # nop/oracle: model and queue collapse to the one nop_oracle id, regardless
    # of whatever (often arbitrary) model string was passed.
    for agent in ("oracle-v2", "nop-baseline", "agent-nop", "agent-oracle-2"):
        for model in (None, "default", "nop_oracle", "some-random-thing"):
            assert (
                settings.normalize_trial_model(agent, model) == NOP_ORACLE_QUEUE_KEY
            ), (agent, model)
            assert (
                settings.get_queue_key_for_trial(agent, model) == NOP_ORACLE_QUEUE_KEY
            ), (agent, model)


def test_non_baseline_agents_are_not_treated_as_nop_oracle(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Substring matches that are not baseline variants must keep normal routing.
    assert settings.get_queue_key_for_trial("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )
    assert settings.normalize_trial_model("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )


def test_nop_oracle_queue_has_separate_default_concurrency(monkeypatch):
    settings = _settings(
        monkeypatch,
        default_model_concurrency=8,
    )

    assert settings.get_model_concurrency("default") == 8
    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 1024
    assert NOP_ORACLE_QUEUE_KEY in settings.get_known_queue_keys()


def test_model_concurrency_overrides_can_override_nop_oracle_queue(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        f'{{"{NOP_ORACLE_QUEUE_KEY}": 12, "default": 3}}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 12
    assert settings.get_model_concurrency("default") == 3


def test_bare_claude_uses_configured_default_but_explicit_prefix_wins(monkeypatch):
    settings = _settings(monkeypatch, claude_code_force_direct_api=False)
    expected = "global.anthropic.claude-sonnet-4-6"
    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6") == expected
    )
    assert (
        settings.get_provider_for_trial("claude-code", "claude-sonnet-4-6") == "bedrock"
    )
    assert (
        settings.get_queue_key_for_trial("claude-code", "claude-sonnet-4-6") == expected
    )
    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-sonnet-4-6")
        == "anthropic/claude-sonnet-4-6"
    )
    assert settings.get_provider_for_trial("claude-code", None) == "bedrock"


@pytest.mark.parametrize("force_direct", [False, True])
def test_default_qa_model_routes_luna_without_changing_summaries(
    monkeypatch, force_direct
):
    monkeypatch.delenv("ODDISH_ANALYSIS_MODEL", raising=False)
    monkeypatch.delenv("ODDISH_QA_MODEL", raising=False)
    settings = _settings(
        monkeypatch, claude_code_force_direct_api=force_direct, openai_provider="openai"
    )
    assert settings.analysis_model == "claude-sonnet-5"
    assert settings.qa_model == "openai/gpt-6-luna"
    assert settings.get_qa_queue_key() == "openai/gpt-6-luna"
    assert (
        settings.normalize_trial_model("claude-code", settings.qa_model)
        == settings.qa_model
    )
    assert (
        settings.get_provider_for_trial("claude-code", settings.qa_model) == "openai"
    )


def test_qa_model_preserves_legacy_override_and_prefers_qa_override(monkeypatch):
    monkeypatch.delenv("ODDISH_QA_MODEL", raising=False)
    monkeypatch.setenv("ODDISH_ANALYSIS_MODEL", "anthropic/claude-sonnet-5")
    settings = _settings(monkeypatch, claude_code_force_direct_api=False)
    assert settings.analysis_model == "anthropic/claude-sonnet-5"
    assert settings.qa_model == "anthropic/claude-sonnet-5"
    assert settings.get_qa_queue_key() == "anthropic/claude-sonnet-5"
    monkeypatch.setenv("ODDISH_QA_MODEL", "openai/gpt-6-sol")
    settings = _settings(monkeypatch)
    assert settings.qa_model == "openai/gpt-6-sol"
    assert settings.get_qa_queue_key() == "openai/gpt-6-sol"
    assert settings.analysis_model == "anthropic/claude-sonnet-5"


def test_anthropic_hdo_prefix_has_a_separate_queue(monkeypatch):
    settings = _settings(monkeypatch, claude_code_force_direct_api=True)
    expected = "anthropic-hdo/claude-sonnet-4-6"
    assert settings.normalize_trial_model("claude-code", expected) == expected
    assert settings.get_provider_for_trial("claude-code", expected) == "anthropic-hdo"
    assert settings.get_queue_key_for_trial("claude-code", expected) == expected
    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6")
        == "anthropic/claude-sonnet-4-6"
    )


@pytest.mark.parametrize(
    "alias,api_id,bedrock_id",
    [
        ("claude-opus-4-8", "claude-opus-4-8", "global.anthropic.claude-opus-4-8"),
        ("claude-opus-4.8", "claude-opus-4-8", "global.anthropic.claude-opus-4-8"),
        (
            "claude-sonnet-4.6",
            "claude-sonnet-4-6",
            "global.anthropic.claude-sonnet-4-6",
        ),
        ("claude-opus-5-5", "claude-opus-5-5", "global.anthropic.claude-opus-5-5"),
        ("claude-opus-5.5", "claude-opus-5-5", "global.anthropic.claude-opus-5-5"),
        ("claude-fable-5", "claude-fable-5", "global.anthropic.claude-fable-5"),
        ("claude-fable-5-1", "claude-fable-5-1", "global.anthropic.claude-fable-5-1"),
    ],
)
def test_claude_aliases_keep_the_selected_provider(
    monkeypatch, alias, api_id, bedrock_id
):
    settings = _settings(monkeypatch, claude_code_force_direct_api=True)
    assert settings.normalize_trial_model("claude-code", alias) == f"anthropic/{api_id}"
    assert (
        settings.normalize_trial_model("claude-code", f"anthropic/{alias}")
        == f"anthropic/{api_id}"
    )
    assert (
        settings.normalize_trial_model("claude-code", f"bedrock/{alias}") == bedrock_id
    )
    assert settings.normalize_queue_key(f"anthropic/{alias}") == f"anthropic/{api_id}"
    assert settings.normalize_queue_key(f"bedrock/{alias}") == bedrock_id
    assert settings.normalize_queue_key(bedrock_id) == bedrock_id


def test_openrouter_claude_model_routes_through_openrouter(monkeypatch):
    settings = _settings(monkeypatch)

    # An explicit openrouter/ prefix must pin the trial to OpenRouter instead
    # of being rewritten to a Bedrock inference-profile id.
    model = "openrouter/anthropic/claude-opus-4.8"

    assert settings.normalize_trial_model("claude-code", model) == model
    assert settings.get_provider_for_trial("claude-code", model) == "openrouter"
    assert settings.get_queue_key_for_trial("claude-code", model) == model
    assert settings.normalize_queue_key(model) == model


def test_glm_model_routes_to_zai_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # GLM runs on the claude-code harness but must NOT inherit claude-code's
    # fixed Bedrock provider/queue -- it gets its own z.ai bucket so it does not
    # contend with heavy Bedrock traffic for concurrency slots.
    for raw in (
        "glm-x-preview[1m]",
        "zai/glm-x-preview[1m]",
        "z-ai/glm-x-preview[1m]",
        "GLM-X-Preview[1M]",
    ):
        assert (
            settings.normalize_trial_model("claude-code", raw)
            == "zai/glm-x-preview[1m]"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "zai", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw)
            == "zai/glm-x-preview[1m]"
        ), raw


def test_minimax_model_routes_to_minimax_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # MiniMax runs on the claude-code harness but must get its own provider /
    # queue bucket (the canonical id is lowercased for storage/queueing).
    for raw in ("MiniMax-M3", "minimax/MiniMax-M3", "minimax-m3"):
        assert (
            settings.normalize_trial_model("claude-code", raw) == "minimax/minimax-m3"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "minimax", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw) == "minimax/minimax-m3"
        ), raw


def test_moonshot_model_routes_to_moonshot_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    for raw in (
        "kimi-k2.7-code",
        "moonshot/kimi-k2.7-code",
        "kimi/kimi-k2.7-code",
        "moonshotai/kimi-k2.7-code",
    ):
        assert (
            settings.normalize_trial_model("claude-code", raw)
            == "moonshot/kimi-k2.7-code"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "moonshot", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw)
            == "moonshot/kimi-k2.7-code"
        ), raw


def test_deepseek_model_routes_to_deepseek_provider(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    cases = {
        "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro-0813": "deepseek/deepseek-v4-pro",
        "ds/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
        "deepseek-flash": "deepseek/deepseek-flash",
        "deepseek/deepseek-v4.1-flash": "deepseek/deepseek-flash",
    }
    for raw, canonical in cases.items():
        assert settings.normalize_trial_model("dsh", raw) == canonical, raw
        assert settings.get_provider_for_trial("dsh", raw) == "deepseek", raw
        assert settings.get_queue_key_for_trial("dsh", raw) == canonical, raw


def test_fireworks_models_route_to_fireworks_not_direct_providers(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # The consolidation route: an explicit ``fireworks/`` prefix sends GLM /
    # MiniMax / Kimi to one shared Fireworks bucket instead of each model's own
    # direct provider. Friendly spellings collapse to the canonical short id so
    # every spelling shares one queue/provider bucket.
    cases = {
        "fireworks/glm-5.2": "fireworks/glm-5p2",
        "fw/glm-5p2": "fireworks/glm-5p2",
        "fireworks/minimax-m3": "fireworks/minimax-m3",
        "fireworks/kimi-k2.7": "fireworks/kimi-k2p7-code",
        "fireworks/kimi-k2.7-code": "fireworks/kimi-k2p7-code",
        "fireworks/kimi-k2p7-code": "fireworks/kimi-k2p7-code",
        "fireworks/deepseek-v4.1-flash": "fireworks/deepseek-v4p1-flash",
        "fw/deepseek-v4p1-flash": "fireworks/deepseek-v4p1-flash",
    }
    for raw, canonical in cases.items():
        assert settings.normalize_trial_model("claude-code", raw) == canonical, raw
        assert settings.get_provider_for_trial("claude-code", raw) == "fireworks", raw
        assert settings.get_queue_key_for_trial("claude-code", raw) == canonical, raw


def test_grok_build_xai_model_routes_to_xai(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "xai/redacted-model"

    assert normalize_model_id(" XAI / redacted-model ") == model
    assert settings.normalize_trial_model("grok-build", model) == model
    assert settings.get_provider_for_trial("grok-build", model) == "xai"
    assert settings.get_queue_key_for_trial("grok-build", model) == model
    assert settings.normalize_queue_key(model) == model


def test_grok_build_public_xai_route_keeps_its_credential_identity(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "xai-public/grok-4.7"

    assert settings.normalize_trial_model("grok-build", model) == model
    assert settings.get_provider_for_trial("grok-build", model) == "xai-public"
    assert settings.get_queue_key_for_trial("grok-build", model) == model
    assert settings.normalize_queue_key(model) == model


def test_meta_model_routes_to_meta_for_mini_swe_agent(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "meta/llama-eval-model"

    assert normalize_model_id(" Meta / Llama Eval Model ") == model
    assert settings.normalize_trial_model("mini-swe-agent", model) == model
    assert settings.get_provider_for_trial("mini-swe-agent", model) == "meta"
    assert settings.get_queue_key_for_trial("mini-swe-agent", model) == model
    assert settings.normalize_queue_key(model) == model


def test_meta_public_model_keeps_its_own_provider_identity(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "meta-public/muse-spark-1.3"

    assert normalize_model_id(" Meta-Public / Muse Spark 1.3 ") == model
    for agent in ("muse-code", "tbh", "mini-swe-agent"):
        assert settings.normalize_trial_model(agent, model) == model
        assert settings.get_provider_for_trial(agent, model) == "meta-public"
        assert settings.get_queue_key_for_trial(agent, model) == model
    assert settings.normalize_queue_key(model) == model
    assert infer_model_provider_prefix(model) == "meta-public"


def test_meta_public_prefix_is_not_the_internal_meta_route():
    assert is_meta_public_model("meta-public/muse-spark-1.3")
    assert is_meta_public_model("META-PUBLIC/muse-spark-1.3")
    assert not is_meta_model("meta-public/muse-spark-1.3")
    assert not is_meta_public_model("meta/goofy_glacier135")
    assert not is_meta_public_model(None)
    assert meta_public_bare_model_id("meta-public/muse-spark-1.3") == "muse-spark-1.3"
    assert (
        to_meta_public_model_id("META-PUBLIC/muse-spark-1.3")
        == "meta-public/muse-spark-1.3"
    )


def test_meta_harness_model_id_maps_only_the_public_route():
    assert meta_harness_model_id("meta-public/muse-spark-1.3") == "meta/muse-spark-1.3"
    assert meta_harness_model_id("meta/goofy_glacier135") == "meta/goofy_glacier135"
    assert meta_harness_model_id("xai-public/grok-4.7") == "xai-public/grok-4.7"
    assert meta_harness_model_id(None) is None


def test_meta_public_api_key_is_a_settings_field(monkeypatch):
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-key")
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.meta_public_api_key == "public-key"


def test_resolve_meta_public_api_key_prefers_settings_then_env(monkeypatch):
    monkeypatch.setenv("META_PUBLIC_API_KEY", "env-key")

    def resolve(value):
        return resolve_meta_public_api_key(SimpleNamespace(meta_public_api_key=value))

    assert resolve("settings-key") == "settings-key"
    assert resolve(None) == "env-key"
    assert resolve("   ") == "env-key"
    monkeypatch.setenv("META_PUBLIC_API_KEY", "   ")
    assert resolve(None) is None
    monkeypatch.delenv("META_PUBLIC_API_KEY")
    assert resolve(None) is None


def test_meta_agent_env_includes_configured_session_controls(monkeypatch):
    monkeypatch.setenv("ODDISH_META_EVAL_NAME", "SWE Marathon")
    monkeypatch.setenv("ODDISH_META_SESSION_ID", "swe-marathon--123456")
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_meta_agent_env()

    assert env["ODDISH_META_EVAL_NAME"] == "SWE Marathon"
    assert env["ODDISH_META_SESSION_ID"] == "swe-marathon--123456"
    assert env["MSWEA_API_KEY"] == "${META_API_KEY}"
    # LiteLLM's openai/ provider authenticates from OPENAI_API_KEY.
    assert env["OPENAI_API_KEY"] == "${META_API_KEY}"
    assert "OPENAI_API_BASE" not in env


def test_geometric_model_routes_to_geometric_for_mini_swe_agent(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "geometric/glm-5.3"

    assert normalize_model_id(" Geometric / GLM-5.3 ") == model
    assert settings.normalize_trial_model("mini-swe-agent", model) == model
    assert settings.get_provider_for_trial("mini-swe-agent", model) == "geometric"
    assert settings.get_queue_key_for_trial("mini-swe-agent", model) == model
    assert settings.normalize_queue_key(model) == model


def test_geometric_gm_alias_collapses_to_one_queue_key(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Both spellings must share a single stored id / concurrency bucket.
    assert (
        settings.normalize_trial_model("mini-swe-agent", "gm/glm-5.3")
        == "geometric/glm-5.3"
    )
    assert (
        settings.get_provider_for_trial("mini-swe-agent", "gm/glm-5.3") == "geometric"
    )


def test_bare_glm_still_routes_to_zai_not_geometric(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Geometric is prefix-only: taking the bare GLM ids would silently reroute
    # every existing z.ai trial. An explicit prefix is the only opt-in.
    assert settings.normalize_trial_model("claude-code", "glm-5.3") == "zai/glm-5.3"
    assert settings.get_provider_for_trial("claude-code", "glm-5.3") == "zai"
    assert settings.normalize_trial_model("claude-code", "zai/glm-5.3") == "zai/glm-5.3"


def test_geometric_rejects_a_model_the_endpoint_does_not_serve():
    # A typo must die at submit, not after a queue slot, worker, and sandbox
    # have been spent reaching the endpoint's 404.
    with pytest.raises(ValueError, match="glm-5.3"):
        require_geometric_served_model_id("geometric/glm-5.4")


def test_geometric_cannot_smuggle_a_foreign_model_to_public_openai():
    # The real hazard: geometric/gpt-4o would reach litellm as ``openai/gpt-4o``,
    # whose default route is public OpenAI. Only OPENAI_BASE_URL keeps it on our
    # own box, so refuse the id rather than depend on that env var.
    with pytest.raises(ValueError):
        require_geometric_served_model_id("geometric/gpt-4o")


def test_geometric_normalization_is_total_over_stored_rows(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # _GEOMETRIC_SERVED_MODELS is meant to change with --served-model-name, so an
    # id valid when a trial was written can later leave the set. Every read over
    # stored rows must still resolve. get_provider_for_trial and
    # get_queue_key_for_trial do NOT expose ``strict``, so normalization has to
    # be total rather than relying on callers to opt out.
    retired = "geometric/glm-5.2"
    assert settings.normalize_trial_model("mini-swe-agent", retired) == retired
    assert (
        settings.normalize_trial_model("mini-swe-agent", retired, strict=False)
        == retired
    )
    assert settings.get_provider_for_trial("mini-swe-agent", retired) == "geometric"
    assert settings.get_queue_key_for_trial("mini-swe-agent", retired) == retired


def test_geometric_canonicalizes_case_to_the_served_model_name(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # The wire id must match --served-model-name exactly whatever case is typed.
    assert (
        settings.normalize_trial_model("mini-swe-agent", "geometric/GLM-5.3")
        == "geometric/glm-5.3"
    )
    assert require_geometric_served_model_id("geometric/GLM-5.3") == "glm-5.3"


def test_geometric_allowlist_leaves_other_providers_open(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # The allowlist is justified by Geometric being a single-model endpoint; it
    # must not leak into the multi-model vendor APIs, which stay open.
    assert (
        settings.normalize_trial_model("mini-swe-agent", "meta/anything-at-all")
        == "meta/anything-at-all"
    )
    assert settings.normalize_trial_model("claude-code", "zai/glm-5.4") == "zai/glm-5.4"


def test_blank_geometric_base_urls_normalize_to_unset(monkeypatch):
    # A key present-and-empty in a Modal secret passes str validation as "",
    # which would otherwise resolve to no egress host and an empty endpoint.
    # Blank means unset, so both routes stay on the default.
    monkeypatch.setenv("GEOMETRIC_BASE_URL", "  ")
    monkeypatch.setenv("GEOMETRIC_ANTHROPIC_BASE_URL", "")
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.geometric_base_url == GEOMETRIC_DEFAULT_BASE_URL
    assert settings.geometric_anthropic_base_url is None
    assert settings.get_geometric_agent_env()["OPENAI_BASE_URL"] == (
        GEOMETRIC_DEFAULT_BASE_URL
    )
    assert settings.get_geometric_anthropic_base_url() == (
        GEOMETRIC_DEFAULT_BASE_URL.removesuffix("/v1")
    )


def test_geometric_agent_env_points_mini_swe_at_geometric(monkeypatch):
    monkeypatch.setenv("GEOMETRIC_BASE_URL", "https://api.geometric.example/v1/")
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_geometric_agent_env()

    assert env["MSWEA_API_KEY"] == "${GEOMETRIC_API_KEY}"
    # LiteLLM's openai/ provider authenticates from OPENAI_API_KEY.
    assert env["OPENAI_API_KEY"] == "${GEOMETRIC_API_KEY}"
    # Trailing slash trimmed, matching the Meta route.
    assert env["OPENAI_BASE_URL"] == "https://api.geometric.example/v1"
    assert "OPENAI_API_BASE" not in env
    # Both verified necessary against a live endpoint: without MSWEA_CONFIGURED
    # mini-swe-agent drops into an interactive setup wizard and aborts with no
    # TTY; without MSWEA_COST_TRACKING it raises on a model litellm has no
    # price for, which a self-hosted GLM-5.3 always is.
    assert env["MSWEA_CONFIGURED"] == "true"
    assert env["MSWEA_COST_TRACKING"] == "ignore_errors"


def test_grok_provider_prefix_canonicalizes_to_xai(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert (
        settings.normalize_trial_model("grok-build", "grok/redacted-model")
        == "xai/redacted-model"
    )
    assert (
        settings.get_queue_key_for_trial("grok-build", "grok/redacted-model")
        == "xai/redacted-model"
    )
    assert settings.get_provider_for_trial("grok-build", "grok/redacted-model") == "xai"


def test_grok_build_without_model_uses_xai_provider_bucket(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.get_provider_for_trial("grok-build", None) == "xai"
    assert settings.get_queue_key_for_trial("grok-build", None) == "xai"


def test_bare_glm_minimax_kimi_keep_direct_provider_routes(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Without the ``fireworks/`` prefix the existing per-vendor direct routes are
    # unchanged -- adding Fireworks must not hijack them.
    assert settings.get_provider_for_trial("claude-code", "glm-x-preview[1m]") == "zai"
    assert settings.get_provider_for_trial("claude-code", "minimax-m3") == "minimax"
    assert (
        settings.get_provider_for_trial("claude-code", "kimi-k2.7-code") == "moonshot"
    )


def test_fireworks_queue_keys_have_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"fireworks/glm-5p2": 4, "fireworks/kimi-k2p7-code": 6}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency("fireworks/glm-5p2") == 4
    assert settings.get_model_concurrency("fireworks/kimi-k2p7-code") == 6


def test_openrouter_kimi_model_is_not_hijacked_to_moonshot(monkeypatch):
    settings = _settings(monkeypatch)

    # An explicit openrouter/ prefix keeps OpenRouter routing -- the direct
    # Moonshot path must not steal it (both columns run concurrently).
    model = "openrouter/moonshotai/kimi-k2.7-code"

    assert settings.normalize_trial_model("claude-code", model) == model
    assert settings.get_provider_for_trial("claude-code", model) == "openrouter"
    assert settings.get_queue_key_for_trial("claude-code", model) == model


def test_minimax_moonshot_queue_keys_have_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"minimax/minimax-m3": 4, "moonshot/kimi-k2.7-code": 6}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency("minimax/minimax-m3") == 4
    assert settings.get_model_concurrency("moonshot/kimi-k2.7-code") == 6


def test_glm_queue_key_has_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"zai/glm-x-preview[1m]": 4, "global.anthropic.claude-opus-4-8": 64}',
    )
    settings = Settings(_env_file=None)

    # The GLM bucket is keyed separately from any Bedrock model, so capping GLM
    # concurrency does not throttle (and is not throttled by) Bedrock trials.
    assert settings.get_model_concurrency("zai/glm-x-preview[1m]") == 4
    assert settings.get_model_concurrency("global.anthropic.claude-opus-4-8") == 64


def test_legacy_unmapped_claude_queue_key_does_not_break_reads(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    legacy_key = "bedrock/claude-sonnet-4-6-20250514"

    assert settings.normalize_queue_key(legacy_key) == legacy_key
    with pytest.raises(ValueError):
        settings.normalize_trial_model("claude-code", legacy_key)


def test_openai_provider_defaults_to_azure(monkeypatch):
    settings = _settings(monkeypatch)

    assert settings.get_openai_provider() == "azure"
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="openai/gpt-5.2")


def test_openai_api_key_alone_does_not_enable_public_openai(monkeypatch):
    settings = _settings(monkeypatch, openai_api_key="sk-test")

    assert settings.get_openai_provider() == "azure"
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="openai/gpt-5.2")


def test_azure_openai_deployment_mapping_accepts_prefixed_and_bare_models(
    monkeypatch,
):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={
            "openai/gpt-5.2": "azure-gpt-5-2",
            "gpt-5.4": "azure-gpt-5-4",
        },
    )

    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )
    assert settings.resolve_azure_openai_deployment("gpt-5.2") == "azure-gpt-5-2"
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.4") == (
        "azure-gpt-5-4"
    )


def test_azure_openai_deployment_mapping_normalizes_model_keys(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{" OpenAI / GPT 5.2 ":"azure-gpt-5-2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.azure_openai_deployments == {"openai/gpt-5.2": "azure-gpt-5-2"}
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )


def test_missing_azure_openai_deployment_mapping_fails_loudly(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        settings.resolve_azure_openai_deployment("openai/gpt-5.4")


def test_openai_queue_key_preserves_requested_model_name(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    assert settings.normalize_trial_model("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )
    assert settings.get_queue_key_for_trial("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )


def test_azure_openai_runtime_env_excludes_public_openai_key(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_openai_runtime_env(model="openai/gpt-5.2")

    assert env["AZURE_OPENAI_API_KEY"] == "az-key"
    assert env["AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert env["AZURE_OPENAI_DEPLOYMENT"] == "oddish-gpt"
    assert env["OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert "OPENAI_API_KEY" not in env


def test_azure_openai_agent_env_uses_compatible_base_url(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_openai_agent_env(model="openai/gpt-5.2")

    assert env["OPENAI_API_KEY"] == "az-key"
    assert env["OPENAI_BASE_URL"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_KEY"] == "az-key"
    assert env["AZURE_API_BASE"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_VERSION"] == "2025-01-01-preview"


def test_foundry_openai_v1_endpoint_is_allowed(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"gpt-5.2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert (
        settings.get_azure_openai_base_url()
        == "https://example.services.ai.azure.com/openai/v1"
    )


def test_azure_openai_agent_env_rejects_foundry_project_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/oddish",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    with pytest.raises(RuntimeError, match="Do not use the Foundry project endpoint"):
        settings.get_openai_agent_env(model="openai/gpt-5.2")


def test_public_openai_requires_explicit_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = _settings(
        monkeypatch,
        clear_openai_env=False,
        openai_provider="openai",
    )

    assert settings.get_openai_provider() == "openai"
    assert settings.get_openai_runtime_env() == {"OPENAI_API_KEY": "sk-test"}


@pytest.mark.parametrize(
    ("model", "api_id"),
    [
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1"),
        # An unmapped regional id keeps its name instead of raising.
        ("us.anthropic.claude-unknown-9-v1:0", "us.anthropic.claude-unknown-9-v1:0"),
        ("anthropic/claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ],
)
def test_bedrock_inference_profiles_map_to_the_api_id(model, api_id):
    assert to_anthropic_api_model_id(model) == api_id
