"""Haiku 5.5 routing regressions."""

import pytest

from oddish.config import Settings


@pytest.mark.parametrize(
    "model", ["claude-haiku-5-5", "claude-haiku-5.5", "bedrock/claude-haiku-5-5"]
)
def test_haiku_55_bedrock_route(model):
    settings = Settings(_env_file=None, claude_code_force_direct_api=False)
    expected = "global.anthropic.claude-haiku-5-5"
    assert settings.normalize_trial_model("claude-code", model) == expected
    assert settings.get_queue_key_for_trial("claude-code", model) == expected
    assert settings.get_provider_for_trial("claude-code", model) == "bedrock"


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "anthropic/claude-haiku-5-5"])
def test_haiku_55_direct_api_route(model):
    settings = Settings(_env_file=None, claude_code_force_direct_api=True)
    expected = "anthropic/claude-haiku-5-5"
    assert settings.normalize_trial_model("claude-code", model) == expected
    assert settings.get_provider_for_trial("claude-code", model) == "anthropic"
