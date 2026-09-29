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


@pytest.mark.parametrize("child", [False, True], ids=["worker", "child"])
@pytest.mark.parametrize(
    "declares_key,platform_key",
    [(False, "platform-judge-key"), (True, "platform-judge-key"), (True, None)],
    ids=["no-judge", "judge", "missing-key"],
)
def test_bedrock_agent_preserves_declared_anthropic_verifier_key(
    monkeypatch, tmp_path, make_task, child, declares_key, platform_key
):
    task_path = make_task()
    with (task_path / "task.toml").open("a") as config:
        config.write("\nbuild_timeout_sec = 120.0\n")
    if declares_key:
        with (task_path / "task.toml").open("a") as config:
            config.write(
                '\n[verifier.env]\nANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"\n'
            )
    task = Task(task_path)
    if platform_key is None:
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
        payload = _build_payload(
            task_path=task_path,
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent="claude-code",
            model="global.anthropic.claude-opus-5-5",
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
                model="global.anthropic.claude-opus-5-5",
                jobs_dir=tmp_path / "jobs",
                environment=EnvironmentType.DOCKER,
            )
        )
        error = outcome.error

    agent_env = next(env for command, env in commands if "claude --verbose" in command)
    assert agent_env["ANTHROPIC_API_KEY"] == ""
    assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert agent_env["AWS_BEARER_TOKEN_BEDROCK"] == "aws-agent-key"
    assert os.environ.get("ANTHROPIC_API_KEY") == platform_key
    if platform_key is None:
        assert "ANTHROPIC_API_KEY' not found in host environment" in error
        assert not trial_paths.reward_text_path.exists()
    else:
        assert error is None
