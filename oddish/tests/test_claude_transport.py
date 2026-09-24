"""Provider intent survives worker routing and credential overlays."""

from __future__ import annotations

import pytest

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.agents.factory import AgentFactory
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment

from oddish.config import settings
from oddish.core.llm_key_fingerprint import hash_llm_key, trial_llm_key_hash
from oddish.worker.local_runner import _bedrock_agent_env
from oddish.workers.harbor.agent_config import _build_routed_agent_config
from oddish.workers.harbor.agent_config import _build_agent_config
from oddish.workers.harbor.ephemeral import _build_payload
from oddish.workers.harbor._entry import _build_job_config
from oddish.workers.harbor.model_hosts import outbound_hosts_for_model
from oddish.workers.queue.byok import uses_direct_anthropic
from oddish.workers.queue.job_tokens import scoped_model_env
from oddish.workers.queue.model_gateway import is_gateway_trial


API = "anthropic/claude-sonnet-4-5"
BEDROCK = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/AbCd123"
)


@pytest.fixture
def command_environment(tmp_path, monkeypatch):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM python:3.13-slim\n")
    environment = DockerEnvironment(
        environment_dir=environment_dir,
        environment_name="routing-test",
        session_id="routing-test",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=TaskEnvironmentConfig(),
        persistent_env={"ANTHROPIC_MODEL": "claude-wrong-persistent"},
    )
    calls = []

    async def capture(command, env=None, **_kwargs):
        # Only sandbox execution is mocked. Use Harbor's actual merge and
        # scoped agent env instead of reproducing their precedence in the test.
        calls.append((command, environment._merge_env(env) or {}))
        return ExecResult(return_code=0, stdout="", stderr="")

    monkeypatch.setattr(environment, "exec", capture)
    return environment, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ["claude-code", "kimi-claude-code"])
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "requested,expected,provider,host",
    [
        (
            BEDROCK_ARN,
            BEDROCK_ARN,
            "bedrock",
            "bedrock-runtime.us-east-1.amazonaws.com",
        ),
        (API, "claude-sonnet-4-5", None, "api.anthropic.com"),
    ],
)
async def test_claude_command_uses_selected_model_and_provider_allowlist(
    tmp_path,
    monkeypatch,
    command_environment,
    agent_name,
    child,
    requested,
    expected,
    provider,
    host,
):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "platform-ant")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-wrong-ambient")
    if child:
        payload = _build_payload(
            task_path=tmp_path,
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent=agent_name,
            model=requested,
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config={},
            is_probe=False,
            extra_agent_env={"ANTHROPIC_MODEL": "claude-wrong-child"},
        )
        config = _build_job_config(payload).agents[0]
    else:
        config = _build_agent_config(
            agent=agent_name, model=requested, raw_harbor_config={}
        )
    agent = AgentFactory.create_agent_from_config(config, tmp_path)
    environment, calls = command_environment
    with environment.scoped_exec_env(agent.extra_env):
        await agent.run("hello", environment, object())
    command, final_env = calls[-1]
    assert "claude --verbose" in command
    assert agent.model_name == expected
    assert final_env["ANTHROPIC_MODEL"] == expected
    assert final_env["CLAUDE_CODE_USE_BEDROCK"] == (
        "1" if provider == "bedrock" else ""
    )
    assert final_env["ANTHROPIC_API_KEY"] == (
        "" if provider == "bedrock" else "platform-ant"
    )
    assert agent.to_agent_info().model_info.provider == provider
    assert host in outbound_hosts_for_model(requested, agent_env=config.env)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["sigv4", "profile", "bearer", "missing"])
@pytest.mark.parametrize("child", [False, True])
async def test_mini_swe_bedrock_forwards_aws_provider_chain(
    tmp_path, monkeypatch, command_environment, auth, child
):
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "MSWEA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    expected = {}
    if auth == "sigv4":
        expected = {
            "AWS_ACCESS_KEY_ID": "aws-access",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_SESSION_TOKEN": "aws-session",
            "AWS_REGION": "us-east-1",
        }
    elif auth == "profile":
        credentials = tmp_path / "credentials"
        credentials.write_text("[trial-profile]\naws_access_key_id=profile-access\naws_secret_access_key=profile-secret\naws_session_token=profile-session\n")
        monkeypatch.setenv("AWS_PROFILE", "trial-profile")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        expected = {"AWS_REGION": "us-east-1"}
    elif auth == "bearer":
        expected = {"AWS_BEARER_TOKEN_BEDROCK": "aws-bearer", "AWS_REGION": "us-east-1"}
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    if child:
        payload = _build_payload(
            task_path=tmp_path,
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent="mini-swe-agent",
            model=BEDROCK,
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config={},
            is_probe=False,
        )
        config = _build_job_config(payload).agents[0]
    else:
        config = _build_agent_config(
            agent="mini-swe-agent", model=BEDROCK, raw_harbor_config={}
        )
    agent = AgentFactory.create_agent_from_config(config, tmp_path)
    environment, calls = command_environment
    with environment.scoped_exec_env(agent.extra_env):
        await agent.run("hello", environment, object())
    command, final_env = calls[-1]
    assert f"--model=bedrock/{BEDROCK_ID}" in command
    for key, value in expected.items():
        assert final_env[key] == value
    if auth == "profile":
        assert final_env["AWS_ACCESS_KEY_ID"] == "profile-access"
        assert final_env["AWS_SECRET_ACCESS_KEY"] == "profile-secret"
        assert final_env["AWS_SESSION_TOKEN"] == "profile-session"
        assert not final_env.get("AWS_SHARED_CREDENTIALS_FILE")
        assert not final_env.get("AWS_PROFILE")
    if auth == "bearer":
        assert final_env["MSWEA_API_KEY"] == "aws-bearer"
    if auth == "missing":
        # Harbor's one-key gate must let the AWS SDK try its default chain
        # (including instance roles) without fabricating access keys.
        assert not final_env.get("AWS_ACCESS_KEY_ID")
        assert not final_env.get("MSWEA_API_KEY")
    assert final_env["ANTHROPIC_API_KEY"] == ""


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("ambient_key", [False, True])
@pytest.mark.parametrize(
    "requested,expected,bedrock",
    [
        (API, "claude-sonnet-4-5", False),
        ("claude/claude-sonnet-4-5", "claude-sonnet-4-5", False),
        (BEDROCK, BEDROCK_ID, True),
    ],
)
def test_explicit_claude_route_survives_ambient_credentials(
    monkeypatch, flag, ambient_key, requested, expected, bedrock
):
    monkeypatch.setattr(settings, "claude_code_force_direct_api", flag)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    if ambient_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-ant")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    routed = _build_routed_agent_config(
        agent="claude-code", model=requested, raw_harbor_config={}
    )
    assert routed.model_name == expected
    assert routed.env.get("CLAUDE_CODE_USE_BEDROCK") == ("1" if bedrock else "")
    if bedrock:
        assert routed.env.get("ANTHROPIC_API_KEY") == ""


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize(
    "requested,expected,bedrock",
    [
        (API, "claude-sonnet-4-5", False),
        ("claude/claude-sonnet-4-5", "claude-sonnet-4-5", False),
        (BEDROCK, BEDROCK_ID, True),
    ],
)
def test_ephemeral_child_model_and_env_follow_explicit_route(
    tmp_path, monkeypatch, flag, requested, expected, bedrock
):
    monkeypatch.setattr(settings, "claude_code_force_direct_api", flag)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-ant")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    payload = _build_payload(
        task_path=tmp_path,
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json",
        agent="claude-code",
        model=requested,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={},
        is_probe=True,
        extra_agent_env={"ANTHROPIC_API_KEY": "user-ant"},
    )
    env = {
        **payload["agent_config"]["env"],
        **payload["runtime_env"],
        **payload["extra_agent_env"],
    }
    assert payload["model"] == expected
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == ("1" if bedrock else "")
    assert env.get("ANTHROPIC_API_KEY") == ("" if bedrock else "user-ant")


@pytest.mark.parametrize("flag", [False, True])
def test_litellm_bedrock_keeps_provider_and_scopes_no_anthropic_key(monkeypatch, flag):
    monkeypatch.setattr(settings, "claude_code_force_direct_api", flag)
    monkeypatch.setattr(settings, "anthropic_api_key", "platform-ant")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-ant")
    routed = _build_routed_agent_config(
        agent="mini-swe-agent", model=BEDROCK, raw_harbor_config={}
    )
    assert routed.model_name == f"bedrock/{BEDROCK_ID}"
    bundle = scoped_model_env(
        agent="mini-swe-agent",
        model=BEDROCK,
        settings=settings,
        byok_env={"ANTHROPIC_API_KEY": "user-ant"},
    )
    assert "ANTHROPIC_API_KEY" not in bundle


def test_bedrock_byok_does_not_claim_anthropic_funding(monkeypatch):
    monkeypatch.setattr(settings, "claude_code_force_direct_api", True)
    assert not uses_direct_anthropic("claude-code", BEDROCK, settings=settings)
    assert trial_llm_key_hash(
        "bedrock", {"ANTHROPIC_API_KEY": "user-ant"}
    ) != hash_llm_key("user-ant")


def test_explicit_claude_routes_are_not_sent_to_mixed_qa_gateway():
    args = dict(kind="qa", agent="claude-code", byok_env=None, harbor_config={})
    assert not is_gateway_trial(**args, model="anthropic/claude-sonnet-5")
    assert not is_gateway_trial(
        **args, model="bedrock/global.anthropic.claude-sonnet-5"
    )
    assert is_gateway_trial(**args, model="anthropic-hdo/claude-sonnet-5")


def test_local_bedrock_env_clears_competing_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-ant")
    env = _bedrock_agent_env(BEDROCK)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_platform_key_from_settings_reaches_anthropic_child_without_ambient_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "settings-ant")
    payload = _build_payload(
        task_path=tmp_path,
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json",
        agent="claude-code",
        model=API,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={},
        is_probe=False,
    )
    assert payload["runtime_env"]["ANTHROPIC_API_KEY"] == "settings-ant"
    assert payload["model"] == "claude-sonnet-4-5"


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("credentials", ["none", "anthropic", "aws", "both"])
@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("requested", [API, BEDROCK])
def test_child_final_selectors_follow_explicit_route_after_conflicting_overlays(
    tmp_path, monkeypatch, flag, credentials, byok, requested
):
    monkeypatch.setattr(settings, "claude_code_force_direct_api", flag)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    if credentials in {"anthropic", "both"}:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-ant")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if credentials in {"aws", "both"}:
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "aws-token")
    else:
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    extra = {
        "ANTHROPIC_BASE_URL": "https://opposing.example/api",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_BEDROCK": "1" if requested == API else "",
    }
    if byok:
        extra["ANTHROPIC_API_KEY"] = "user-ant"
    payload = _build_payload(
        task_path=tmp_path,
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json",
        agent="claude-code",
        model=requested,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={},
        is_probe=False,
        extra_agent_env=extra,
    )
    child = _build_job_config(payload).agents[0]
    assert child.env["ANTHROPIC_BASE_URL"] == ""
    assert child.env["CLAUDE_CODE_USE_VERTEX"] == ""
    if requested == API:
        assert child.model_name == "claude-sonnet-4-5"
        assert child.env["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert child.env.get("ANTHROPIC_API_KEY") == (
            "user-ant"
            if byok
            else "ambient-ant" if credentials in {"anthropic", "both"} else None
        )
    else:
        assert child.model_name == BEDROCK_ID
        assert child.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert child.env["ANTHROPIC_API_KEY"] == ""


@pytest.mark.parametrize("agent_name", ["claude-code", "kimi-claude-code"])
def test_hdo_keeps_its_dedicated_key_on_claude_adapters(monkeypatch, agent_name):
    monkeypatch.setattr(settings, "anthropic_hdo_api_key", "hdo-test")
    config = _build_agent_config(
        agent=agent_name, model="anthropic-hdo/claude-opus-4-8", raw_harbor_config={}
    )
    assert config.model_name == "anthropic-hdo/claude-opus-4-8"
    assert config.env["ANTHROPIC_API_KEY"] == "hdo-test"
    assert config.env["CLAUDE_CODE_USE_BEDROCK"] == ""


def test_anthropic_child_mini_swe_uses_required_wrapper(tmp_path):
    from oddish.workers.agents.mini_swe_agent import OddishMiniSweAgent

    payload = _build_payload(
        task_path=tmp_path, jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json", agent="mini-swe-agent", model=API,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={}, is_probe=False,
    )
    agent = AgentFactory.create_agent_from_config(_build_job_config(payload).agents[0], tmp_path)
    assert isinstance(agent, OddishMiniSweAgent)
    assert "_skip_mcp_handler: true" in agent._oddish_config_yaml(None)


def test_web_identity_is_resolved_before_sandbox_transfer(monkeypatch):
    import os
    import boto3
    from types import SimpleNamespace
    from oddish.workers.queue.job_tokens import bedrock_sandbox_env

    before = dict(os.environ)
    def session():
        assert os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/worker/token"
        assert os.environ["AWS_ROLE_ARN"] == "arn:aws:iam::123:role/test"
        return SimpleNamespace(region_name="us-east-1", get_credentials=lambda: SimpleNamespace(
            get_frozen_credentials=lambda: SimpleNamespace(access_key="temporary-access", secret_key="temporary-secret", token="temporary-token")
        ))
    monkeypatch.setattr(boto3, "Session", session)
    env = bedrock_sandbox_env({"AWS_WEB_IDENTITY_TOKEN_FILE": "/worker/token", "AWS_ROLE_ARN": "arn:aws:iam::123:role/test"})
    assert env == {"AWS_ACCESS_KEY_ID": "temporary-access", "AWS_SECRET_ACCESS_KEY": "temporary-secret", "AWS_SESSION_TOKEN": "temporary-token", "AWS_DEFAULT_REGION": "us-east-1"}
    assert dict(os.environ) == before
