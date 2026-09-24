"""ABT-1335: the input provider survives storage, queueing and re-normalization."""

import pytest

from oddish.config import Settings


@pytest.mark.parametrize("agent", ["claude-code", "mini-swe-agent", "terminus-2"])
@pytest.mark.parametrize("force_direct", [False, True])
@pytest.mark.parametrize(
    "requested,canonical,provider",
    [
        ("anthropic/claude-opus-4-8", "anthropic/claude-opus-4-8", "anthropic"),
        ("claude/claude-opus-4-8", "anthropic/claude-opus-4-8", "anthropic"),
        ("anthropic/claude-opus-4.8", "anthropic/claude-opus-4-8", "anthropic"),
        (
            "anthropic/claude-3.5-haiku-20241022",
            "anthropic/claude-3-5-haiku-20241022",
            "anthropic",
        ),
        (
            "anthropic/claude-haiku-4-5-20251001",
            "anthropic/claude-haiku-4-5-20251001",
            "anthropic",
        ),
        (
            "anthropic/claude-api-only-test",
            "anthropic/claude-api-only-test",
            "anthropic",
        ),
        (
            "anthropic/global.anthropic.claude-opus-4-8",
            "anthropic/claude-opus-4-8",
            "anthropic",
        ),
        ("bedrock/claude-opus-4-8", "global.anthropic.claude-opus-4-8", "bedrock"),
        (
            "bedrock/global.anthropic.claude-opus-4-8",
            "global.anthropic.claude-opus-4-8",
            "bedrock",
        ),
        ("anthropic.claude-opus-4-8", "global.anthropic.claude-opus-4-8", "bedrock"),
        *[
            (
                f"{region}.anthropic.claude-opus-4-8",
                f"{region}.anthropic.claude-opus-4-8",
                "bedrock",
            )
            for region in ("global", "us", "eu", "apac", "apn")
        ],
        (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/AbCd123",
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/AbCd123",
            "bedrock",
        ),
        (
            "bedrock/arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/AbCd123",
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/AbCd123",
            "bedrock",
        ),
        (
            "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/AbCd123",
            "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/AbCd123",
            "bedrock",
        ),
    ],
)
def test_explicit_route_survives_canonicalization(
    agent, force_direct, requested, canonical, provider
):
    settings = Settings(_env_file=None, claude_code_force_direct_api=force_direct)
    assert settings.normalize_trial_model(agent, requested) == canonical
    assert settings.get_provider_for_trial(agent, requested) == provider
    assert settings.get_queue_key_for_trial(agent, requested) == canonical
    assert settings.normalize_queue_key(requested) == canonical
    # A worker may have a different default from the submitting API.
    settings.claude_code_force_direct_api = not force_direct
    assert settings.normalize_trial_model(agent, canonical) == canonical
    assert settings.get_provider_for_trial(agent, canonical) == provider
    assert settings.normalize_queue_key(canonical) == canonical


@pytest.mark.parametrize(
    "force_direct,canonical",
    [(True, "anthropic/claude-opus-4-8"), (False, "global.anthropic.claude-opus-4-8")],
)
def test_bare_default_is_resolved_before_storage(force_direct, canonical, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None, claude_code_force_direct_api=force_direct)
    assert settings.normalize_trial_model("claude-code", "claude-opus-4-8") == canonical
    assert settings.normalize_queue_key("claude-opus-4-8") == canonical


@pytest.mark.parametrize("force_direct", [False, True])
@pytest.mark.parametrize(
    "model,queue_key",
    [
        ("claude-sonnet-4-5", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ("claude-3-5-sonnet-20241022", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
    ],
)
def test_historical_response_preserves_stored_identity(
    monkeypatch, force_direct, model, queue_key
):
    from datetime import datetime, timezone

    from oddish.config import settings
    from oddish.core.helpers import build_trial_response
    from oddish.db import TrialModel
    from oddish.schemas import TrialOrigin, TrialStatus

    monkeypatch.setattr(settings, "claude_code_force_direct_api", force_direct)
    trial = TrialModel(
        id="historical-claude",
        name="historical",
        task_id="task",
        agent="claude-code",
        model=model,
        provider="bedrock",
        queue_key=queue_key,
        status=TrialStatus.SUCCESS,
        origin=TrialOrigin.ODDISH,
        attempts=1,
        max_attempts=1,
        created_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        cost_usd=0.125,
    )

    response = build_trial_response(trial, "s3://tasks/historical")

    assert response.model == model
    assert response.provider == "bedrock"
    assert response.queue_key == queue_key
    assert response.cost_usd == 0.125
    assert trial.model == model


def test_provider_concurrency_limits_are_independent():
    settings = Settings(
        _env_file=None,
        model_concurrency_overrides={
            "anthropic/claude-opus-4-8": 2,
            "global.anthropic.claude-opus-4-8": 7,
        },
    )
    assert settings.get_model_concurrency("anthropic/claude-opus-4-8") == 2
    assert (
        settings.get_model_concurrency("bedrock/global.anthropic.claude-opus-4-8") == 7
    )


@pytest.mark.parametrize("model", ["anthropic/", "claude/", "bedrock/"])
def test_empty_explicit_model_is_rejected(model):
    with pytest.raises(ValueError, match="model"):
        Settings(_env_file=None).normalize_trial_model("claude-code", model)


def test_pricing_prefers_the_selected_provider_rate(monkeypatch):
    import litellm
    from oddish.model_pricing import _find_litellm_pricing, get_model_pricing

    # Distinct synthetic prices catch falling back to the model-family price
    # before trying the selected provider's exact catalog entry.
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "claude-opus-4-8": {"input_cost_per_token": 1, "output_cost_per_token": 2},
            "anthropic/claude-opus-4-8": {
                "input_cost_per_token": 3,
                "output_cost_per_token": 4,
            },
            "bedrock/global.anthropic.claude-opus-4-8": {
                "input_cost_per_token": 5,
                "output_cost_per_token": 6,
            },
        },
    )
    _find_litellm_pricing.cache_clear()
    try:
        assert get_model_pricing("anthropic/claude-opus-4-8").input == 3
        assert get_model_pricing("global.anthropic.claude-opus-4-8").input == 5
    finally:
        _find_litellm_pricing.cache_clear()


@pytest.mark.parametrize(
    "region,suffix",
    [
        ("us-west-1", "amazonaws.com"),
        ("cn-north-1", "amazonaws.com.cn"),
        ("us-gov-west-1", "amazonaws.com"),
    ],
)
def test_bedrock_allowlist_includes_the_executed_aws_region(region, suffix):
    from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

    settings = Settings(_env_file=None)
    model = settings.normalize_trial_model("mini-swe-agent", "bedrock/claude-opus-4-8")
    hosts = outbound_hosts_for_model(model, agent_env={"AWS_REGION": region})
    assert f"bedrock-runtime.{region}.{suffix}" in hosts
    assert f"sts.{region}.{suffix}" in hosts
    assert "api.anthropic.com" not in hosts
    direct = settings.normalize_trial_model(
        "mini-swe-agent", "anthropic/claude-opus-4-8"
    )
    hosts = outbound_hosts_for_model(direct, agent_env={"AWS_REGION": region})
    assert "api.anthropic.com" in hosts
    assert not any("amazonaws" in host for host in hosts)


@pytest.mark.parametrize("infer", [False, True])
def test_bedrock_allowlist_covers_arn_and_sdk_region_precedence(infer):
    from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

    hosts = outbound_hosts_for_model(
        "arn:aws:bedrock:us-west-1:123456789012:application-inference-profile/AbCd123",
        agent_env={"AWS_REGION": "us-east-2", "AWS_REGION_NAME": "eu-west-3"},
        infer_bare_provider=infer,
    )
    for region in ("us-west-1", "us-east-2", "eu-west-3"):
        assert f"bedrock-runtime.{region}.amazonaws.com" in hosts
    assert "api.anthropic.com" not in hosts


@pytest.mark.parametrize(
    "region", ["evil.example/path", "evil.example:443", "*.evil.example"]
)
def test_aws_region_cannot_inject_an_unrelated_allowlist_host(region):
    from oddish.workers.agents.network import normalize_domain_or_url
    from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

    hosts = outbound_hosts_for_model(
        "global.anthropic.claude-opus-4-8", agent_env={"AWS_REGION": region}
    )
    assert all(
        normalize_domain_or_url(host).endswith(".amazonaws.com") for host in hosts
    )


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("provider,expected", [
    ("anthropic", "anthropic/claude-opus-4-8"),
    ("bedrock", "global.anthropic.claude-opus-4-8"),
])
def test_stored_bare_model_uses_persisted_provider(flag, provider, expected):
    settings = Settings(_env_file=None, claude_code_force_direct_api=flag)
    assert settings.model_for_stored_trial("claude-code", "claude-opus-4-8", provider) == expected
