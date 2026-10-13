"""Automatic monitoring uses platform routing without copying secret values."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import endpoint_health as health
from oddish.config import Settings


@pytest.fixture
def platform(monkeypatch):
    import oddish.config
    import oddish.core.model_concurrency

    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    config = Settings(_env_file=None)
    config.model_concurrency_overrides = {}
    monkeypatch.setattr(oddish.config, "settings", config)
    stored = AsyncMock(return_value={})
    monkeypatch.setattr(
        oddish.core.model_concurrency, "get_model_concurrency_overrides", stored
    )
    return config, stored


@pytest.mark.asyncio
async def test_discovery_merges_limits_excludes_disabled_and_deduplicates(platform):
    config, stored = platform
    config.model_concurrency_overrides = {
        "xai/grok-4.6": 8,
        "xai/vendor-latest-learnability": 4,
    }
    stored.return_value = {
        "xai/grok-4.6": 0,
        "google/gemini-3.5-flash": 4,
        "gemini/gemini-3.5-flash": 4,
        "nop_oracle": 100,
    }
    targets = await health.platform_targets(None)
    assert {t.model for t in targets} == {
        "xai/vendor-latest-learnability",
        "gemini/gemini-3.5-flash",
    }
    assert {t.api_key_env for t in targets} == {"XAI_API_KEY", "GEMINI_API_KEY"}


@pytest.mark.asyncio
async def test_index_maps_aliases_to_their_shared_connection(platform):
    _, stored = platform
    stored.return_value = {
        "google/gemini-3.5-flash": 4,
        "gemini/gemini-3.5-flash": 4,
        "xai/grok-4.6": 8,
    }
    targets, target_ids = await health.platform_target_index(None)
    assert len(targets) == 2
    assert (
        target_ids["google/gemini-3.5-flash"] == target_ids["gemini/gemini-3.5-flash"]
    )
    assert set(target_ids.values()) == {t.id for t in targets}


@pytest.mark.asyncio
async def test_monitor_cap_applies_to_registration_not_the_index(platform):
    _, stored = platform
    stored.return_value = {f"xai/grok-test-{i}": 1 for i in range(101)}
    targets, target_ids = await health.platform_target_index(None)
    assert len(targets) == len(target_ids) == 101
    with pytest.raises(ValueError, match="At most 100"):
        await health.platform_targets(None)


@pytest.mark.asyncio
async def test_discovery_uses_azure_mapping_not_every_azure_deployment(platform):
    config, stored = platform
    config.openai_provider = "azure"
    config.azure_openai_deployments = {
        "openai/gpt-test": "enterprise-test",
        "openai/image-test": "image-only",
    }
    stored.return_value = {"openai/gpt-test": 8}
    (target,) = await health.platform_targets(None)
    assert target.model == "azure/enterprise-test"
    assert target.api_key_env == "AZURE_OPENAI_API_KEY"
    assert target.api_base_env == "AZURE_OPENAI_ENDPOINT"
    assert target.api_version_env == "AZURE_OPENAI_API_VERSION"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,transport,key",
    [
        ("xai/grok-4.6", "xai/grok-4.6", "XAI_API_KEY"),
        ("xai-public/grok-4", "xai/grok-4", "XAI_PUBLIC_API_KEY"),
        (
            "anthropic-hdo/claude-sonnet-5",
            "anthropic/claude-sonnet-5",
            "ANTHROPIC_HDO_API_KEY",
        ),
        ("minimax/minimax-m3", "anthropic/MiniMax-M3", "MINIMAX_API_KEY"),
        ("zai/glm-5.3", "anthropic/glm-5.3", "ZAI_API_KEY"),
        ("deepseek/deepseek-v4-pro", "openai/deepseek-v4-pro", "DEEPSEEK_API_KEY"),
        ("ds/deepseek-v4.1-flash", "openai/deepseek-flash", "DEEPSEEK_API_KEY"),
        (
            "fireworks/glm-5p3",
            "anthropic/accounts/fireworks/models/glm-5p3",
            "FIREWORKS_API_KEY",
        ),
        ("relace/glm-5.3", "anthropic/z-ai/glm-5.3", "RELACE_API_KEY"),
        ("meta-public/muse-spark-1.3", "openai/muse-spark-1.3", "META_PUBLIC_API_KEY"),
        (
            "meta-public-2/muse-spark-1.3",
            "openai/muse-spark-1.3",
            "META_PUBLIC_2_API_KEY",
        ),
        (
            "hunyuan/hy4-preview-offline",
            "openai/hy4-preview-offline",
            "HUNYUAN_API_KEY",
        ),
    ],
)
async def test_discovery_keeps_provider_and_credential_identity(
    platform, model, transport, key
):
    _, stored = platform
    stored.return_value = {model: 8}
    (target,) = await health.platform_targets(None)
    assert target.model == transport
    assert target.api_key_env == key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured",
    [None, "https://models.relace.ai/v1", "https://models.relace.ai/v1/"],
)
async def test_discovery_relace_base_matches_trials(platform, monkeypatch, configured):
    _, stored = platform
    stored.return_value = {"relace/glm-5.3": 8}
    if configured is None:
        monkeypatch.delenv("RELACE_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("RELACE_BASE_URL", configured)
    (target,) = await health.platform_targets(None)
    assert target.api_base == "https://models.relace.ai"
    assert target.api_base_env is None


@pytest.mark.asyncio
async def test_discovery_bedrock_uses_existing_aws_region(platform, monkeypatch):
    _, stored = platform
    stored.return_value = {"global.anthropic.claude-sonnet-5": 8}
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    (target,) = await health.platform_targets(None)
    assert target.model == "bedrock/global.anthropic.claude-sonnet-5"
    assert target.api_key_env is None
    assert target.region == "us-west-2"


@pytest.mark.asyncio
async def test_discovery_custom_base_and_missing_key_are_not_success(
    platform, monkeypatch
):
    import litellm

    _, stored = platform
    stored.return_value = {"zai/glm-5.3": 8}
    monkeypatch.setenv("ZAI_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    call = AsyncMock()
    monkeypatch.setattr(litellm, "acompletion", call)
    (target,) = await health.platform_targets(None)
    assert target.api_base_env == "ZAI_BASE_URL"
    result = await health.check_endpoint(target)
    assert result.outcome == "monitor_error"
    assert result.error == "Missing ZAI_API_KEY configuration."
    call.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_deepseek_uses_openai_compatible_base(platform, monkeypatch):
    _, stored = platform
    stored.return_value = {"deepseek/deepseek-v4-pro": 8}
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    (target,) = await health.platform_targets(None)
    assert target.api_base == "https://api.deepseek.com"
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    (target,) = await health.platform_targets(None)
    assert target.api_base is None
    assert target.api_base_env == "DEEPSEEK_BASE_URL"


@pytest.mark.asyncio
async def test_discovery_unsupported_route_is_reported(platform):
    _, stored = platform
    stored.return_value = {"cursor/example": 8}
    (target,) = await health.platform_targets(None)
    result = await health.check_endpoint(target)
    assert result.outcome == "monitor_error"
    assert "No automatic completion check" in result.error


@pytest.mark.asyncio
async def test_probe_uses_default_base_and_normalizes_azure_base(monkeypatch):
    import litellm

    call = AsyncMock(
        return_value=SimpleNamespace(
            id="request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        )
    )
    monkeypatch.setattr(litellm, "acompletion", call)
    monkeypatch.setenv("TEST_KEY", "sentinel")
    target = health.EndpointTarget(
        name="Custom",
        model="anthropic/test",
        api_key_env="TEST_KEY",
        api_base="https://example.invalid/anthropic",
    )
    assert (await health.check_endpoint(target)).outcome == "success"
    assert call.call_args.kwargs["api_base"] == "https://example.invalid/anthropic"
    monkeypatch.setenv("TEST_BASE", "https://example.invalid/openai/v1/")
    monkeypatch.setenv("TEST_VERSION", "test-version")
    target = health.EndpointTarget(
        name="Azure",
        model="azure/test",
        api_key_env="TEST_KEY",
        api_base_env="TEST_BASE",
        api_version_env="TEST_VERSION",
    )
    assert (await health.check_endpoint(target)).outcome == "success"
    assert call.call_args.kwargs["api_base"] == "https://example.invalid"


def test_new_base_field_preserves_existing_monitor_identity():
    target = health.EndpointTarget(name="Old", model="xai/test", api_key_env="TEST_KEY")
    previous = {
        "model": "xai/test",
        "api_key_env": "TEST_KEY",
        "api_base_env": None,
        "api_version_env": None,
        "region": None,
    }
    assert (
        target.id
        == hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()[
            :32
        ]
    )
    assert (
        target.model_copy(update={"api_base": "https://example.invalid"}).id
        != target.id
    )


@pytest.mark.asyncio
async def test_openai_reasoning_check_uses_completion_token_budget(monkeypatch):
    import litellm

    call = AsyncMock(
        return_value=SimpleNamespace(
            id="request",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        )
    )
    monkeypatch.setattr(litellm, "acompletion", call)
    monkeypatch.setenv("TEST_KEY", "sentinel")
    target = health.EndpointTarget(
        name="Luna", model="openai/gpt-6-luna", api_key_env="TEST_KEY"
    )
    assert (await health.check_endpoint(target)).outcome == "success"
    assert call.call_args.kwargs["max_completion_tokens"] == 1024
    assert "max_tokens" not in call.call_args.kwargs
    call.side_effect = litellm.BadRequestError(
        message="Invalid check sentinel", model=target.model, llm_provider="openai"
    )
    result = await health.check_endpoint(target)
    assert result.outcome == "monitor_error"
    assert result.status_code == 400
    assert "sentinel" not in result.error


@pytest.mark.asyncio
async def test_missing_azure_mapping_is_visible_as_monitor_error(platform):
    config, stored = platform
    config.openai_provider = "azure"
    config.azure_openai_deployments = {}
    stored.return_value = {"openai/gpt-missing": 8}
    (target,) = await health.platform_targets(None)
    result = await health.check_endpoint(target)
    assert result.outcome == "monitor_error"
    assert "No Azure deployment mapping" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queue_key",
    ["x" * 150, "my.provider/model", "/no-provider", "xai/" + "g" * 300],
)
async def test_invalid_queue_key_is_reported_without_dropping_others(
    platform, queue_key
):
    _, stored = platform
    stored.return_value = {queue_key: 4, "xai/grok-4.6": 8}
    targets = await health.platform_targets(None)
    by_name = {t.name: t for t in targets}
    assert by_name["xai/grok-4.6"].api_key_env == "XAI_API_KEY"
    (bad,) = (t for t in targets if t.name != "xai/grok-4.6")
    assert bad.configuration_error
    assert len(bad.name) <= 100
