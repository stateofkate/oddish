"""``build_sandbox_env``: the monitor sandbox's environment, assembled from what
the launcher minted and resolved. No database, no ambient process env."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oddish.mon.env import AGENT_ENV_VAR, API_URL_VAR, REGISTRY_AUTH_VAR, TOKEN_VAR
from oddish.workers.launcher import launch_env
from oddish.workers.queue import byok, job_tokens

PREPARED = SimpleNamespace(
    trial_agent="claude-code",
    trial_model="anthropic/claude-sonnet-5",
    trial_harbor_config={"agent_config": {"name": "claude-code"}},
)


@pytest.fixture
def scoped(monkeypatch):
    """Record what the model-env scoping is asked for; answer with fixed keys
    so the composition, not the scoping, is under test."""
    calls: dict = {}

    def _scoped(**kwargs):
        calls["scoped"] = kwargs
        return {"ANTHROPIC_API_KEY": "platform", "SHARED": "scoped"}

    def _ambient(agent, ambient):
        calls["ambient"] = (agent, dict(ambient))
        return {"CURSOR_API_KEY": ambient.get("CURSOR_API_KEY", "")}

    def _merge(byok_env, _other):
        calls["merge"] = byok_env
        return {"SHARED": "user"} if byok_env else {}

    monkeypatch.setattr(job_tokens, "scoped_model_env", _scoped)
    monkeypatch.setattr(job_tokens, "agent_ambient_env", _ambient)
    monkeypatch.setattr(byok, "merge_byok_env", _merge)
    return calls


def test_the_env_carries_the_handle_the_agent_keys_and_the_judge_keys(scoped):
    env = launch_env.build_sandbox_env(
        PREPARED,
        api_url="https://api.test",
        credential="job-1.1.secret",
        byok_env={"ANTHROPIC_API_KEY": "user"},
        judge_env={"OPENAI_API_KEY": "judge"},
        ambient={"CURSOR_API_KEY": "cursor", "HOME": "/root"},
    )

    assert env[API_URL_VAR] == "https://api.test"
    assert env[TOKEN_VAR] == "job-1.1.secret"
    assert env["OPENAI_API_KEY"] == "judge"
    assert REGISTRY_AUTH_VAR not in env
    # The user's key wins over the platform key; the harness key comes from
    # the launcher's own environment, selected by agent.
    assert json.loads(env[AGENT_ENV_VAR]) == {
        "ANTHROPIC_API_KEY": "platform",
        "SHARED": "user",
        "CURSOR_API_KEY": "cursor",
    }
    assert scoped["scoped"]["agent"] == "claude-code"
    assert scoped["scoped"]["model"] == "anthropic/claude-sonnet-5"
    assert scoped["scoped"]["raw_harbor_config"] == PREPARED.trial_harbor_config
    assert scoped["scoped"]["byok_env"] == {"ANTHROPIC_API_KEY": "user"}
    # The ambient env passed in is the one used, not the process's own.
    assert scoped["scoped"]["ambient"] == {"CURSOR_API_KEY": "cursor", "HOME": "/root"}
    assert scoped["ambient"] == (
        "claude-code",
        {"CURSOR_API_KEY": "cursor", "HOME": "/root"},
    )


def test_registry_logins_are_serialised_only_when_given(scoped):
    creds = [SimpleNamespace(to_dict=lambda: {"registry": "r", "user": "u"})]

    env = launch_env.build_sandbox_env(
        PREPARED,
        api_url="https://api.test",
        credential="tok",
        byok_env=None,
        judge_env={},
        registry_credentials=creds,
        ambient={},
    )

    assert json.loads(env[REGISTRY_AUTH_VAR]) == [{"registry": "r", "user": "u"}]
    assert json.loads(env[AGENT_ENV_VAR]) == {
        "ANTHROPIC_API_KEY": "platform",
        "SHARED": "scoped",
        "CURSOR_API_KEY": "",
    }
    assert scoped["merge"] is None


def test_without_an_ambient_env_the_process_env_is_used(scoped, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "from-process")

    env = launch_env.build_sandbox_env(
        PREPARED, api_url="u", credential="t", byok_env=None, judge_env={}
    )

    assert json.loads(env[AGENT_ENV_VAR])["CURSOR_API_KEY"] == "from-process"


def test_a_public_meta_sandbox_gets_only_the_public_key(monkeypatch):
    # Real scoping: the launcher's worker holds both Meta keys, and the
    # sandbox must get the public one under the Meta names, never the internal.
    monkeypatch.setattr(launch_env.settings, "meta_api_key", "internal-key")
    monkeypatch.setattr(launch_env.settings, "meta_public_api_key", "public-key")
    monkeypatch.setenv("META_API_KEY", "internal-key")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-key")
    prepared = SimpleNamespace(
        trial_agent="muse-code",
        trial_model="meta-public/muse-spark-1.3",
        trial_harbor_config={"agent_config": {"name": "muse-code"}},
    )

    env = launch_env.build_sandbox_env(
        prepared,
        api_url="https://api.test",
        credential="tok",
        byok_env=None,
        judge_env={},
        ambient={"META_API_KEY": "internal-key", "META_PUBLIC_API_KEY": "public-key"},
    )

    agent_env = json.loads(env[AGENT_ENV_VAR])
    assert agent_env["META_API_KEY"] == "public-key"
    assert agent_env["META_PUBLIC_API_KEY"] == "public-key"
    assert "internal-key" not in json.dumps(env)
