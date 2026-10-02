"""Agent provider selection must not erase a task's declared judge key."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.factory import AgentFactory
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.task import Task
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.verifier import Verifier

from oddish.workers.harbor import _entry, runner
from oddish.workers.harbor.ephemeral import _build_payload


@pytest.mark.parametrize("credential_source", ["environment", "settings"])
@pytest.mark.parametrize("route", ["bedrock", "hdo", "byok"])
@pytest.mark.parametrize("child", [False, True], ids=["worker", "child"])
@pytest.mark.parametrize(
    "declares_key,platform_key",
    [(False, "platform-judge-key"), (True, "platform-judge-key"), (True, None)],
    ids=["no-judge", "judge", "missing-key"],
)
def test_bedrock_agent_preserves_declared_anthropic_verifier_key(
    monkeypatch,
    tmp_path,
    make_task,
    child,
    declares_key,
    platform_key,
    route,
    credential_source,
):
    from oddish.config import settings

    model = {
        "bedrock": "global.anthropic.claude-opus-5-5",
        "hdo": "anthropic-hdo/claude-sonnet-5-5",
        "byok": "anthropic/claude-sonnet-5-5",
    }[route]
    extra_env = {"ANTHROPIC_API_KEY": "user-agent-key"} if route == "byok" else None
    monkeypatch.setattr(settings, "anthropic_hdo_api_key", "hdo-agent-key")
    task_path = make_task()
    with (task_path / "task.toml").open("a") as config:
        config.write("\nbuild_timeout_sec = 120.0\n")
    if declares_key:
        with (task_path / "task.toml").open("a") as config:
            config.write(
                '\n[verifier.env]\nANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"\n'
            )
    task = Task(task_path)
    monkeypatch.setattr(
        settings,
        "anthropic_api_key",
        platform_key if credential_source == "settings" else None,
    )
    if platform_key is None or credential_source == "settings":
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", platform_key)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "aws-agent-key")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.verifier_dir.mkdir(parents=True)
    environment = DockerEnvironment(
        environment_dir=task.paths.environment_dir,
        environment_name="credential-test",
        session_id="credential-test",
        trial_paths=trial_paths,
        task_env_config=task.config.environment,
    )
    commands = []

    async def capture(command, env=None, **_kwargs):
        effective_env = environment._merge_env(env) or {}
        commands.append((command, effective_env))
        if "/tests/test.sh" in command and not command.startswith("chmod"):
            expected = "platform-judge-key" if declares_key else None
            assert effective_env.get("ANTHROPIC_API_KEY") == expected
            trial_paths.reward_text_path.write_text("1")
        return ExecResult(return_code=0, stdout="", stderr="")

    monkeypatch.setattr(environment, "exec", capture)
    monkeypatch.setattr(environment, "download_dir", AsyncMock())

    class Job:
        def __init__(self, config):
            self.config = config
            self.job_dir = tmp_path / "jobs" / "job"
            self.job_dir.mkdir(parents=True)

        @classmethod
        async def create(cls, config):
            return cls(config)

        async def run(self):
            agent = AgentFactory.create_agent_from_config(
                self.config.agents[0], tmp_path / "agent"
            )
            # Harbor scopes these overrides to agent execution; the verifier
            # must see the task's host-resolved env after that scope ends.
            with environment.scoped_exec_env(agent.extra_env):
                await agent.run("hello", environment, SimpleNamespace())
            verifier = Verifier(
                task=task,
                trial_paths=trial_paths,
                environment=environment,
                skip_tests_upload=True,
            )
            result = await verifier.verify()
            assert result.rewards == {"reward": 1.0}
            (self.job_dir / "result.json").write_text("{}")
            return SimpleNamespace()

    if child:
        if declares_key and platform_key is None:
            with pytest.raises(ValueError, match="verifier.*ANTHROPIC_API_KEY"):
                _build_payload(
                    task_path=task_path,
                    jobs_dir=tmp_path / "jobs",
                    outcome_path=tmp_path / "outcome.json",
                    agent="claude-code",
                    model=model,
                    extra_agent_env=extra_env,
                    environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
                    raw_harbor_config={},
                )
            assert commands == []
            return
        payload = _build_payload(
            task_path=task_path,
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent="claude-code",
            model=model,
            extra_agent_env=extra_env,
            task_host_env={"ANTHROPIC_API_KEY": platform_key} if platform_key else {},
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config={},
        )

        async def run_child(payload):
            await Job(_entry._build_job_config(payload)).run()
            return {"error": None}

        monkeypatch.setattr(_entry, "_run", run_child)
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps(payload))
        # The real child entrypoint applies the serialized process overrides.
        # Restore them after the test just as process exit would in production.
        for key in payload["runtime_env"]:
            if key in os.environ:
                monkeypatch.setenv(key, os.environ[key])
            else:
                monkeypatch.delenv(key, raising=False)
        exit_code = _entry.main(["_entry.py", str(payload_path)])
        outcome = json.loads((tmp_path / "outcome.json").read_text())
        assert exit_code == (1 if platform_key is None else 0), outcome
        error = outcome["error"]
    else:
        monkeypatch.setattr(runner, "Job", Job)
        monkeypatch.setattr(
            runner, "_check_local_storage_preflight", lambda *a, **k: None
        )
        monkeypatch.setattr(
            runner,
            "_extract_outcome_from_job_result",
            lambda **kw: SimpleNamespace(error=None),
        )
        outcome = asyncio.run(
            runner.run_harbor_trial_async(
                task_path=task_path,
                agent="claude-code",
                model=model,
                extra_agent_env=extra_env,
                jobs_dir=tmp_path / "jobs",
                environment=EnvironmentType.DOCKER,
            )
        )
        error = outcome.error

    agent_env = next(env for command, env in commands if "claude --verbose" in command)
    if route == "bedrock":
        assert agent_env["ANTHROPIC_API_KEY"] == ""
        assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert agent_env["AWS_BEARER_TOKEN_BEDROCK"] == "aws-agent-key"
    else:
        assert agent_env["ANTHROPIC_API_KEY"] == (
            "hdo-agent-key" if route == "hdo" else "user-agent-key"
        )
        assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == ""
    if not child:
        assert os.environ.get("ANTHROPIC_API_KEY") == (
            platform_key if credential_source == "environment" else None
        )
    if platform_key is None:
        assert "ANTHROPIC_API_KEY' not found in host environment" in error
        assert not trial_paths.reward_text_path.exists()
    else:
        assert error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True], ids=["monitor", "child"])
@pytest.mark.parametrize(
    "task_value,override,expected",
    [
        ("literal", "${ANTHROPIC_API_KEY}", {"ANTHROPIC_API_KEY": "judge-key"}),
        ("${UNAVAILABLE_KEY}", "literal", {}),
    ],
    ids=["override-adds-reference", "override-replaces-reference"],
)
async def test_verifier_overrides_select_effective_task_credentials(
    monkeypatch, tmp_path, child, task_value, override, expected
):
    from harbor.utils.env import resolve_task_env_vars, task_environment
    from oddish.mon.env import load
    from oddish.workers.launcher import launch, launch_env

    (tmp_path / "task.toml").write_text(
        f'[verifier.env]\nKEY = "{task_value}"\n'
        f'[[steps]]\nname = "check"\n[steps.verifier.env]\nKEY = "{task_value}"\n'
    )
    config = {"verifier": {"env": {"KEY": override}}}
    monkeypatch.setattr(launch.settings, "anthropic_api_key", "judge-key")
    if child:
        payload = _build_payload(
            task_path=tmp_path,
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent="claude-code",
            model="anthropic/test",
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config=config,
            task_host_env={"ANTHROPIC_API_KEY": "judge-key"},
        )
        sources = payload["task_host_env"]
    else:
        monkeypatch.setattr(
            launch,
            "resolve_task_directory",
            AsyncMock(return_value=(tmp_path, None, None)),
        )
        monkeypatch.setattr(launch, "begin_judge_costs_if_enabled", AsyncMock())
        prepared = SimpleNamespace(
            task_id="task",
            task_s3_key=None,
            task_path=str(tmp_path),
            trial_agent="claude-code",
            trial_model="anthropic/test",
            trial_harbor_config=config,
        )
        job = SimpleNamespace(id="job", subject_id="trial", worker_id="worker")
        _, _, judge_env = await launch._task_facts(job, prepared, "modal")
        sources = load(
            launch_env.build_sandbox_env(
                prepared,
                api_url="https://api.test",
                credential="test-token",
                byok_env={"ANTHROPIC_API_KEY": "agent-key"},
                judge_env=judge_env,
                ambient={},
            )
        ).task_env
    assert sources == expected
    with task_environment(sources):
        assert resolve_task_env_vars(config["verifier"]["env"]) == {
            "KEY": "judge-key" if expected else "literal"
        }
