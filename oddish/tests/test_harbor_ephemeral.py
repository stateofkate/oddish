"""Test the ephemeral Harbor bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from oddish.workers.harbor.model_hosts import (
    KNOWN_TRANSPORT_BASE_URL_KEYS,
    TBH_BASE_URL_KEYS,
)

from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import EnvironmentConfig
from harbor.trial.hooks import TrialEvent
from harbor.utils.env import resolve_env_vars

from oddish.core.harbor_source import harbor_git_requirement
from oddish.workers.harbor import ephemeral as harbor_ephemeral
from oddish.workers.harbor.agent_config import _build_agent_config
from oddish.workers.harbor._entry import (
    _build_job_config,
    _read_payload_and_unlink,
    _thunder_capacity_error_metadata,
)
from oddish.workers.harbor.ephemeral import (
    HarborOverrideImportError,
    _bridge_event,
    _build_payload,
    _read_outcome,
    run_ephemeral_harbor_trial,
)
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.harbor.restricted_network import RUNTIME_MODEL_NAME_ATTR

_EPHEMERAL_HC = {
    "variant_id": "ephemeral",
    "source": "https://github.com/dot-agi/harbor",
    "resolved_sha": "a" * 40,
}
_VALID_TASK_TOML = (
    "[agent]\ntimeout_sec = 60\n[environment]\nbuild_timeout_sec = 60\n"
    "[verifier]\ntimeout_sec = 60\n"
)


def test_bridge_event_end_with_reward_and_exception():
    ev = _bridge_event(
        {
            "event": "end",
            "trial_id": "t-1",
            "environment_provider": "modal",
            "environment_external_id": "sb-9",
            "result": {
                "verifier_result": {"rewards": {"reward": 1.0}},
                "exception_info": {
                    "exception_type": "AgentTimeoutError",
                    "exception_message": "slow",
                },
            },
        },
        trial_id="t-1",
    )
    assert ev.event == TrialEvent.END
    assert ev.environment is None
    assert ev.environment_external_id == "sb-9"
    assert ev.result.verifier_result.rewards["reward"] == 1.0
    assert ev.result.exception_info.exception_type == "AgentTimeoutError"


@pytest.mark.parametrize(
    "event_name, expected",
    [
        ("START", TrialEvent.START),
        (TrialEvent.AGENT_START.value, TrialEvent.AGENT_START),
        ("AGENT_START", TrialEvent.AGENT_START),
        ("agent_start", TrialEvent.AGENT_START),
        ("ENVIRONMENT_START", TrialEvent.ENVIRONMENT_START),
        ("environment_start", TrialEvent.ENVIRONMENT_START),
        ("VERIFICATION_START", TrialEvent.VERIFICATION_START),
        ("verification_start", TrialEvent.VERIFICATION_START),
        ("AGENT_END", TrialEvent.AGENT_END),
        ("agent_end", TrialEvent.AGENT_END),
        ("END", TrialEvent.END),
        ("CANCEL", TrialEvent.CANCEL),
    ],
)
def test_bridge_event_non_end_has_no_result(event_name, expected):
    ev = _bridge_event({"event": event_name, "trial_id": "t-1"}, trial_id="t-1")
    assert ev.event == expected
    assert ev.result is None
    assert ev.trial_id == "t-1"


def test_read_outcome_missing_outcome_json_is_non_retryable(tmp_path):
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=1,
        duration=2.0,
        stderr="No solution found: harbor requires-python",
        stdout_tail="",
    )
    assert outcome.exception_type == "HarborOverrideImportError"
    assert "No solution found" in outcome.error


def test_read_outcome_without_result_json_is_non_retryable(tmp_path):
    (tmp_path / "outcome.json").write_text(
        json.dumps({"job_dir": str(tmp_path), "job_result_path": None, "error": "boom"})
    )
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=1,
        duration=1.0,
        stderr="",
        stdout_tail="",
    )
    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.error == "boom"


@pytest.mark.parametrize(
    ("exception_type", "provider_error_code"),
    [("SystemExit", 19), ("OSError", "EIO"), ("HTTPError", "server_error")],
)
def test_read_outcome_does_not_trust_arbitrary_exception_codes(
    tmp_path, exception_type, provider_error_code
):
    outcome_path = tmp_path / "outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "job_dir": str(tmp_path),
                "job_result_path": None,
                "error": "child crashed",
                "exception_type": exception_type,
                "provider_error_code": provider_error_code,
                "http_status": 503,
                "retry_after_seconds": 20,
            }
        )
    )

    outcome = _read_outcome(
        outcome_path=outcome_path,
        unique_parent=tmp_path,
        returncode=1,
        duration=1.0,
        stderr="",
        stdout_tail="",
        environment_provider=EnvironmentType.THUNDER.value,
    )

    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.provider_error_code is None
    assert outcome.http_status is None
    assert outcome.retry_after_seconds is None


def test_read_outcome_preserves_exact_typed_thunder_capacity_error(tmp_path):
    outcome_path = tmp_path / "outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "job_dir": str(tmp_path),
                "job_result_path": None,
                "error": "capacity unavailable",
                "exception_type": "CapacityError",
                "provider_error_code": "sandbox_capacity_unavailable",
                "http_status": 503,
                "retry_after_seconds": 20,
            }
        )
    )

    outcome = _read_outcome(
        outcome_path=outcome_path,
        unique_parent=tmp_path,
        returncode=1,
        duration=1.0,
        stderr="",
        stdout_tail="",
        environment_provider=EnvironmentType.THUNDER.value,
    )

    assert outcome.exception_type == "CapacityError"
    assert outcome.provider_error_code == "sandbox_capacity_unavailable"
    assert outcome.http_status == 503
    assert outcome.retry_after_seconds == 20


def test_child_exports_metadata_only_for_typed_thunder_capacity_error():
    class CodedError(RuntimeError):
        code = "sandbox_capacity_unavailable"
        status = 503
        retry_after = 20

    assert _thunder_capacity_error_metadata(SystemExit(19)) == {}
    assert _thunder_capacity_error_metadata(CodedError("not Thunder")) == {}

    from thunder_sandbox import CapacityError

    error = CapacityError(
        "unavailable",
        code="sandbox_capacity_unavailable",
        status=503,
        retry_after=20,
    )
    assert _thunder_capacity_error_metadata(error) == {
        "provider_error_code": "sandbox_capacity_unavailable",
        "http_status": 503,
        "retry_after_seconds": 20,
    }


def test_build_payload_prefers_passed_env_build_multiplier():
    # The runner computes the GKE-sized env-build multiplier and passes it in; it
    # must win over whatever rode in the raw config, so the child JobConfig gets
    # the pod-ready-covering value.
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=EnvironmentConfig(type=EnvironmentType.GKE),
        raw_harbor_config={"environment_build_timeout_multiplier": 2.0},
        environment_build_timeout_multiplier=99.0,
    )
    assert payload["environment_build_timeout_multiplier"] == 99.0


def test_build_payload_falls_back_to_raw_env_build_multiplier():
    # With nothing passed (the off-GKE case), the child keeps the caller's raw
    # multiplier -- behavior-preserving for non-GKE ephemeral trials.
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={"environment_build_timeout_multiplier": 2.0},
    )
    assert payload["environment_build_timeout_multiplier"] == 2.0


def test_build_payload_carries_extra_agent_env():
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={},
        extra_agent_env={"ODDISH_API_KEY": "secret-mint"},
    )
    assert payload["extra_agent_env"]["ODDISH_API_KEY"] == "secret-mint"


def test_build_payload_routes_public_xai_for_stock_child(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "private-xai-key")
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="grok-build",
        model="xai-public/grok-4.7",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
        extra_agent_env={"XAI_PUBLIC_API_KEY": "public-xai-key"},
    )

    assert payload["model"] == "grok-4.7"
    assert payload["extra_agent_env"]["XAI_API_KEY"] == "public-xai-key"
    child_config = _build_job_config(payload)
    assert child_config.agents[0].model_name == "grok-4.7"
    assert child_config.agents[0].env["XAI_API_KEY"] == "public-xai-key"


def test_build_payload_routes_ambient_public_xai_key(monkeypatch):
    monkeypatch.setenv("XAI_PUBLIC_API_KEY", "ambient-public-xai-key")
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="grok-build",
        model="xai-public/grok-4.7",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    assert payload["model"] == "grok-4.7"
    assert payload["extra_agent_env"]["XAI_API_KEY"] == "ambient-public-xai-key"


def test_build_payload_public_xai_fails_closed_without_public_key(monkeypatch):
    monkeypatch.delenv("XAI_PUBLIC_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "private-xai-key")

    with pytest.raises(ValueError, match="requires XAI_PUBLIC_API_KEY"):
        _build_payload(
            task_path=Path("/tmp/task"),
            jobs_dir=Path("/tmp/jobs"),
            outcome_path=Path("/tmp/jobs/outcome.json"),
            agent="grok-build",
            model="xai-public/grok-4.7",
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config=dict(_EPHEMERAL_HC),
        )


@pytest.mark.parametrize("agent", ["muse-code", "tbh"])
def test_build_payload_hands_public_meta_child_the_harness_model(monkeypatch, agent):
    from oddish.config import settings as oddish_settings

    monkeypatch.setattr(oddish_settings, "meta_public_api_key", None)
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-meta-key")

    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent=agent,
        model="meta-public/muse-spark-1.3",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    # Harbor stores the public id; the harness runs the Meta id.
    assert payload["model"] == "meta-public/muse-spark-1.3"
    assert payload["runtime_model"] == "meta/muse-spark-1.3"
    # The payload file names the key; the child resolves it from its own env.
    assert "public-meta-key" not in json.dumps(payload)
    child_agent = _build_job_config(payload).agents[0]
    assert child_agent.model_name == "meta-public/muse-spark-1.3"
    assert getattr(child_agent, RUNTIME_MODEL_NAME_ATTR) == "meta/muse-spark-1.3"
    stored = child_agent.model_dump(mode="json")
    assert stored["model_name"] == "meta-public/muse-spark-1.3"
    assert RUNTIME_MODEL_NAME_ATTR not in stored
    assert "meta/muse-spark-1.3" not in json.dumps(stored)
    assert child_agent.env["META_API_KEY"] == "${META_PUBLIC_API_KEY}"
    resolved = resolve_env_vars(child_agent.env)
    assert resolved["META_API_KEY"] == "public-meta-key"
    assert resolved["META_PUBLIC_API_KEY"] == "public-meta-key"
    assert "internal-meta-key" not in resolved.values()


@pytest.mark.parametrize("agent", ["muse-code", "tbh"])
def test_public_meta_child_agent_runs_the_meta_id_and_reports_the_public_model(
    monkeypatch, tmp_path, agent
):
    from harbor.agents.factory import AgentFactory

    from oddish.config import settings as oddish_settings
    from oddish.workers.harbor.patches import (
        _patch_restricted_network_runtime_fields,
    )

    monkeypatch.setattr(oddish_settings, "meta_public_api_key", None)
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-meta-key")
    # tbh refuses to construct without a build to install.
    monkeypatch.setenv("TBH_DOWNLOAD_URL", "https://tbh.example.test/tbh.tar.gz")
    payload = _build_payload(
        task_path=tmp_path / "task",
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "jobs" / "outcome.json",
        agent=agent,
        model="meta-public/muse-spark-1.3",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )
    # The child applies Oddish's Harbor patches before it builds its JobConfig.
    _patch_restricted_network_runtime_fields()

    running = AgentFactory.create_agent_from_config(
        _build_job_config(payload).agents[0], tmp_path
    )

    assert running.model_name == "meta/muse-spark-1.3"
    model_info = running.to_agent_info().model_info
    assert model_info is not None
    assert (model_info.provider, model_info.name) == ("meta-public", "muse-spark-1.3")
    assert "internal-meta-key" not in running.extra_env.values()


@pytest.mark.parametrize("agent", ["muse-code", "tbh"])
def test_public_meta_child_harbor_trial_runs_the_meta_id_and_stores_the_public_model(
    monkeypatch, tmp_path, make_task, construct_harbor_trial, agent
):
    # The child's JobConfig through Harbor's own Job and Trial construction,
    # with nothing stubbed, after the patch the child applies before it builds
    # that config.
    from oddish.cli.api import trial_result_to_import_spec
    from oddish.config import settings as oddish_settings
    from oddish.workers.harbor.patches import (
        _patch_restricted_network_runtime_fields,
    )

    monkeypatch.setattr(oddish_settings, "meta_public_api_key", None)
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-meta-key")
    # tbh refuses to construct without a build to install.
    monkeypatch.setenv("TBH_DOWNLOAD_URL", "https://tbh.example.test/tbh.tar.gz")
    payload = _build_payload(
        task_path=make_task(),
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json",
        agent=agent,
        model="meta-public/muse-spark-1.3",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )
    _patch_restricted_network_runtime_fields()

    job, trial = asyncio.run(construct_harbor_trial(_build_job_config(payload)))

    # The harness runs the Meta id.
    assert trial.agent.model_name == "meta/muse-spark-1.3"
    # The trial config, the trial's config.json and lock, the result and the
    # job config keep the public id.
    assert trial.config.agent.model_name == "meta-public/muse-spark-1.3"
    trial_config_json = trial.paths.config_path.read_text(encoding="utf-8")
    stored_agent = json.loads(trial_config_json)["agent"]
    assert stored_agent["model_name"] == "meta-public/muse-spark-1.3"
    trial_lock_json = trial.paths.lock_path.read_text(encoding="utf-8")
    locked_agent = json.loads(trial_lock_json)["agent"]
    assert locked_agent["model_name"] == "meta-public/muse-spark-1.3"
    result_json = trial.result.model_dump_json()
    result = json.loads(result_json)
    assert result["config"]["agent"]["model_name"] == "meta-public/muse-spark-1.3"
    assert result["agent_info"]["model_info"] == {
        "name": "muse-spark-1.3",
        "provider": "meta-public",
    }
    job_config_json = job.config.model_dump_json(indent=4, exclude_defaults=True)
    job_agents = json.loads(job_config_json)["agents"]
    assert job_agents[0]["model_name"] == "meta-public/muse-spark-1.3"
    for stored in (trial_config_json, trial_lock_json, result_json, job_config_json):
        assert "meta/muse-spark-1.3" not in stored
        assert RUNTIME_MODEL_NAME_ATTR not in stored
    # So re-importing the downloaded trial files it under the public route.
    imported = trial_result_to_import_spec(trial.result)
    assert imported["model"] == "meta-public/muse-spark-1.3"


@pytest.mark.parametrize(
    ("agent", "model", "child_model"),
    [
        ("muse-code", "meta/goofy_glacier135", "meta/goofy_glacier135"),
        ("grok-build", "xai-public/grok-4.7", "grok-4.7"),
        ("nop", None, None),
    ],
)
def test_build_payload_adds_no_runtime_model_off_the_public_meta_route(
    monkeypatch, agent, model, child_model
):
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")
    monkeypatch.setenv("XAI_PUBLIC_API_KEY", "public-xai-key")

    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent=agent,
        model=model,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    assert payload["model"] == child_model
    assert "runtime_model" not in payload
    assert not hasattr(_build_job_config(payload).agents[0], RUNTIME_MODEL_NAME_ATTR)


def test_build_payload_public_meta_bundle_resolves_to_the_public_key(monkeypatch):
    # The job-scoped bundle is the child's last env layer, over the binding.
    from oddish.config import settings as oddish_settings

    monkeypatch.setattr(oddish_settings, "meta_public_api_key", None)
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "public-meta-key")

    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="muse-code",
        model="meta-public/muse-spark-1.3",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config=dict(_EPHEMERAL_HC),
        extra_agent_env={
            "META_PUBLIC_API_KEY": "public-meta-key",
            "META_API_KEY": "public-meta-key",
            "MSWEA_API_KEY": "public-meta-key",
        },
    )

    child_agent = _build_job_config(payload).agents[0]
    assert child_agent.model_name == "meta-public/muse-spark-1.3"
    assert getattr(child_agent, RUNTIME_MODEL_NAME_ATTR) == "meta/muse-spark-1.3"
    resolved = resolve_env_vars(child_agent.env)
    assert resolved["META_API_KEY"] == "public-meta-key"
    assert "internal-meta-key" not in resolved.values()


def test_build_payload_public_meta_fails_closed_without_public_key(monkeypatch):
    from oddish.config import settings as oddish_settings

    monkeypatch.setattr(oddish_settings, "meta_public_api_key", None)
    monkeypatch.delenv("META_PUBLIC_API_KEY", raising=False)
    monkeypatch.setenv("META_API_KEY", "internal-meta-key")

    with pytest.raises(
        ValueError,
        match=re.escape("meta-public model requires META_PUBLIC_API_KEY on the worker"),
    ):
        _build_payload(
            task_path=Path("/tmp/task"),
            jobs_dir=Path("/tmp/jobs"),
            outcome_path=Path("/tmp/jobs/outcome.json"),
            agent="muse-code",
            model="meta-public/muse-spark-1.3",
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            raw_harbor_config=dict(_EPHEMERAL_HC),
        )


def test_build_payload_carries_agent_config():
    agent_config = {
        "env": {"UV_HTTP_RETRIES": "8"},
        "extra_allowed_hosts": ["astral.sh", "pypi.org"],
        "override_setup_timeout_sec": 1800,
    }
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="mini-swe-agent",
        model="openrouter/tencent/hy3",
        environment=EnvironmentType.MODAL,
        raw_harbor_config={"agent_config": agent_config},
    )
    assert payload["agent_config"] == agent_config


def test_thunder_override_payload_injects_openai_hosts_for_restricted_agent(
    tmp_path, monkeypatch
):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text(
        """schema_version = "1.3"

[environment]
network_mode = "public"

[agent]
network_mode = "allowlist"

[verifier]
network_mode = "no-network"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        harbor_ephemeral,
        "outbound_hosts_for_model",
        lambda *_args, **_kwargs: ["api.openai.com", "ab.chatgpt.com"],
    )
    monkeypatch.setattr(
        harbor_ephemeral,
        "agent_runtime_hosts",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        harbor_ephemeral,
        "_runtime_env_overrides",
        lambda **_kwargs: {"OPENAI_API_KEY": "test-key"},
    )

    payload = _build_payload(
        task_path=task_path,
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "jobs" / "outcome.json",
        agent="codex",
        model="openai/gpt-5.5",
        environment_config=EnvironmentConfig(type=EnvironmentType.THUNDER),
        raw_harbor_config={"agent_config": {"extra_allowed_hosts": []}},
    )

    assert payload["agent_config"]["extra_allowed_hosts"] == [
        "api.openai.com",
        "ab.chatgpt.com",
    ]
    child_config = _build_job_config(payload)
    assert child_config.agents[0].extra_allowed_hosts == [
        "api.openai.com",
        "ab.chatgpt.com",
    ]


@pytest.mark.parametrize(
    "unsupported_shape",
    ["compose", "extra-compose", "kube-chart", "custom-environment"],
)
def test_override_payload_skips_host_injection_for_unsupported_network_shapes(
    tmp_path, monkeypatch, unsupported_shape
):
    task_path = tmp_path / "task"
    environment_dir = task_path / "environment"
    environment_dir.mkdir(parents=True)
    (task_path / "task.toml").write_text(
        """schema_version = "1.3"

[environment]
network_mode = "public"

[agent]
network_mode = "allowlist"

[verifier]
network_mode = "no-network"
""",
        encoding="utf-8",
    )
    environment_config = EnvironmentConfig(type=EnvironmentType.MODAL)
    if unsupported_shape == "compose":
        (environment_dir / "docker-compose.yaml").write_text("services: {}\n")
    elif unsupported_shape == "extra-compose":
        environment_config.extra_docker_compose = [tmp_path / "extra-compose.yaml"]
    elif unsupported_shape == "kube-chart":
        chart = environment_dir / "chart"
        chart.mkdir()
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: test\nversion: 0.1.0\n"
        )
    else:
        environment_config.import_path = "custom.environment:Environment"

    def unexpected_hosts(*_args, **_kwargs):
        raise AssertionError("unsupported task shapes must not infer agent hosts")

    monkeypatch.setattr(harbor_ephemeral, "outbound_hosts_for_model", unexpected_hosts)
    monkeypatch.setattr(harbor_ephemeral, "agent_runtime_hosts", unexpected_hosts)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_runtime_env_overrides",
        lambda **_kwargs: {},
    )

    payload = _build_payload(
        task_path=task_path,
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "jobs" / "outcome.json",
        agent="codex",
        model="openai/gpt-5.5",
        environment_config=environment_config,
        raw_harbor_config={"agent_config": {"extra_allowed_hosts": ["submitted.test"]}},
    )

    assert payload["agent_config"]["extra_allowed_hosts"] == ["submitted.test"]


# Every Anthropic-compatible provider Oddish routes through the claude-code
# harness, with a model id each one's endpoint actually serves.
_COMPAT_PROVIDER_MODELS = [
    "openrouter/anthropic/claude-opus-4.8",
    "fireworks/glm-5p2",
    "zai/glm-4.6",
    "geometric/glm-5.3",
    "minimax/minimax-m3",
    "moonshot/kimi-k2",
]


def _compat_payload(model, *, agent="claude-code", raw_harbor_config=None, **over):
    base = {
        "task_path": Path("/tmp/task"),
        "jobs_dir": Path("/tmp/jobs"),
        "outcome_path": Path("/tmp/jobs/outcome.json"),
        "agent": agent,
        "model": model,
        "environment_config": EnvironmentConfig(type=EnvironmentType.DOCKER),
        "raw_harbor_config": raw_harbor_config or dict(_EPHEMERAL_HC),
    }
    base.update(over)
    return _build_payload(**base)


@pytest.mark.parametrize("model", _COMPAT_PROVIDER_MODELS)
def test_ephemeral_agent_env_matches_in_process_for_compat_providers(model):
    """Which dispatch path ran a trial must not change where the agent dials.

    Without the shared routing, an ephemeral Fireworks/z.ai/Kimi trial reached
    the child with no ``ANTHROPIC_BASE_URL``, so Harbor's claude-code agent took
    its direct-API branch and asked api.anthropic.com for a model that only the
    provider serves.
    """
    raw_harbor_config = dict(_EPHEMERAL_HC)
    in_process = _build_agent_config(
        agent="claude-code",
        model=model,
        raw_harbor_config=dict(raw_harbor_config),
    )
    payload = _compat_payload(model, raw_harbor_config=raw_harbor_config)

    assert payload["agent_config"]["env"] == in_process.env
    assert payload["agent_config"]["env"]["ANTHROPIC_BASE_URL"]
    assert payload["agent_config"]["env"]["ANTHROPIC_AUTH_TOKEN"]


@pytest.mark.parametrize("model", _COMPAT_PROVIDER_MODELS)
def test_ephemeral_agent_kwargs_match_in_process_for_compat_providers(model):
    in_process = _build_agent_config(
        agent="claude-code",
        model=model,
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )
    payload = _compat_payload(model)

    assert payload["agent_config"].get("kwargs", {}) == in_process.kwargs


def test_ephemeral_fireworks_agent_reaches_the_fireworks_endpoint(monkeypatch):
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)

    env = _compat_payload("fireworks/glm-5p2")["agent_config"]["env"]

    assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "${FIREWORKS_API_KEY}"
    assert env["ANTHROPIC_MODEL"] == "accounts/fireworks/models/glm-5p2"
    # Ambient platform credentials are blanked so the Fireworks route wins.
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == ""


def test_thunder_host_injection_uses_routed_child_environment(monkeypatch):
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)
    monkeypatch.setattr(
        harbor_ephemeral, "_supports_auto_restricted_agent_network", lambda **_: True
    )
    observed_envs = []

    def capture_hosts(*_args, **kwargs):
        observed_envs.append(kwargs["agent_env"])
        return ["api.fireworks.ai"]

    monkeypatch.setattr(harbor_ephemeral, "outbound_hosts_for_model", capture_hosts)
    monkeypatch.setattr(harbor_ephemeral, "agent_runtime_hosts", capture_hosts)
    payload = _compat_payload(
        "fireworks/glm-5p2",
        environment_config=EnvironmentConfig(type=EnvironmentType.THUNDER),
        extra_agent_env={"ANTHROPIC_AUTH_TOKEN": "byok-token"},
        raw_harbor_config={
            **_EPHEMERAL_HC,
            "agent_config": {"extra_allowed_hosts": ["submitted.test"]},
        },
    )

    assert len(observed_envs) == 2
    for env in observed_envs:
        assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "byok-token"
    assert payload["agent_config"]["extra_allowed_hosts"] == [
        "submitted.test",
        "api.fireworks.ai",
    ]


def test_ephemeral_compat_env_defers_to_a_submitted_base_url():
    payload = _compat_payload(
        "zai/glm-4.6",
        raw_harbor_config={
            **_EPHEMERAL_HC,
            "agent_config": {"env": {"ANTHROPIC_BASE_URL": "https://custom.example"}},
        },
    )

    assert (
        payload["agent_config"]["env"]["ANTHROPIC_BASE_URL"] == "https://custom.example"
    )


def test_ephemeral_direct_anthropic_trial_keeps_its_submitted_shape():
    """A plain Claude trial has no compat provider, so nothing is shaped in."""
    payload = _compat_payload("claude-opus-4-5")

    assert "ANTHROPIC_BASE_URL" not in payload["agent_config"].get("env", {})
    assert payload["model"] == "claude-opus-4-5"


def test_ephemeral_non_claude_agent_keeps_its_submitted_import_path():
    """Only env/kwargs cross the boundary: the child resolves the agent class."""
    payload = _compat_payload(
        "fireworks/glm-5p2",
        agent="ignored-built-in-name",
        raw_harbor_config={
            **_EPHEMERAL_HC,
            "agent_config": {"import_path": "custom.module:CustomAgent"},
        },
    )

    assert payload["agent_config"]["import_path"] == "custom.module:CustomAgent"
    assert "name" not in payload["agent_config"]


def test_ephemeral_hdo_credential_outranks_a_conflicting_extra_agent_env(
    tmp_path, monkeypatch
):
    """The HDO key is the last word, as it is in process.

    In process the HDO credential is re-applied after probe/BYOK creds
    (`agent_config.py`, `_build_agent_config`'s tail). The child's last layer is
    `extra_agent_env`, so the same credential has to ride there or a probe or
    BYOK `ANTHROPIC_API_KEY` silently authenticates the trial with the wrong key.
    """
    monkeypatch.setattr(harbor_ephemeral.settings, "anthropic_hdo_api_key", None)
    monkeypatch.setenv("ANTHROPIC_HDO_API_KEY", "hdo-secret")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    payload = _compat_payload(
        "anthropic-hdo/claude-opus-4-5",
        extra_agent_env={"ANTHROPIC_API_KEY": "byok-key", "ODDISH_API_KEY": "mint"},
    )
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": payload["model"],
            "environment_config": {},
            "agent_config": payload["agent_config"],
            "verifier": {},
            "artifacts": [],
            "runtime_env": payload["runtime_env"],
            "extra_agent_env": payload["extra_agent_env"],
        }
    )

    env = config.agents[0].env
    assert env["ANTHROPIC_API_KEY"] == "hdo-secret"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # The unrelated probe credential still crosses untouched.
    assert env["ODDISH_API_KEY"] == "mint"


def test_ephemeral_hdo_credential_does_not_clobber_a_supplied_model(monkeypatch):
    """Only the credential is re-applied last, not the model id.

    In process the final HDO write happens after the wrapper has nulled
    `agent_config.name`, so it sets the credential keys alone. Widening the last
    layer to the model would trade one ordering asymmetry for another.
    """
    monkeypatch.setattr(harbor_ephemeral.settings, "anthropic_hdo_api_key", None)
    monkeypatch.setenv("ANTHROPIC_HDO_API_KEY", "hdo-secret")
    payload = _compat_payload(
        "anthropic-hdo/claude-opus-4-5",
        extra_agent_env={"ANTHROPIC_MODEL": "probe-pinned-model"},
    )

    assert payload["extra_agent_env"]["ANTHROPIC_MODEL"] == "probe-pinned-model"
    assert payload["extra_agent_env"]["ANTHROPIC_API_KEY"] == "hdo-secret"


def test_ephemeral_litellm_agent_gets_the_routed_hdo_model():
    """A LiteLLM harness cannot parse the internal `anthropic-hdo/` prefix."""
    payload = _compat_payload("anthropic-hdo/claude-opus-4-5", agent="mini-swe-agent")

    assert payload["model"] == "anthropic/claude-opus-4-5"


def test_ephemeral_claude_code_model_id_follows_the_child_normalization():
    """claude-code's Bedrock/direct id is ``_child_model_name``'s decision.

    The routed builder never overrides it: ``_child_model_id`` returns its
    ``model`` argument unchanged for claude-code, and the payload carries the
    id that the child normalization resolved under the surfaced credential.
    """
    from harbor.models.trial.config import AgentConfig

    from oddish.workers.harbor.ephemeral import _child_model_id

    payload = _compat_payload("anthropic-hdo/claude-opus-4-5")

    assert payload["model"] == "claude-opus-4-5"
    routed = AgentConfig(name="claude-code", model_name="routed-spelling")
    assert _child_model_id(routed, model="as-submitted") == "as-submitted"


@pytest.mark.asyncio
async def test_ephemeral_unroutable_model_settles_as_a_trial_error(tmp_path):
    """A builder failure is a terminal trial error, not an escaped exception.

    The in-process runner builds its configs inside the try that turns failures
    into a `HarborOutcome`; an unserved model must not escape this path as a
    worker-level execution failure instead.
    """
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text(_VALID_TASK_TOML)

    outcome = await run_ephemeral_harbor_trial(
        task_path=task_path,
        agent="claude-code",
        jobs_dir=tmp_path / "jobs",
        model="geometric/glm-0.0-unserved",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        harbor_config=dict(_EPHEMERAL_HC),
    )

    assert outcome.reward is None
    assert outcome.exit_code == -1
    assert outcome.exception_type == "ValueError"
    assert "Geometric serves only" in (outcome.error or "")


@pytest.mark.asyncio
async def test_ephemeral_builder_failure_still_removes_the_patched_task_copy(
    tmp_path, monkeypatch
):
    """A builder failure must not strand the copied task tree.

    A task that patches its task.toml is copied into a temporary directory whose
    cleanup belongs to the try/finally around the child process. Returning an
    outcome before entering that block skips the cleanup.
    """
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text(_VALID_TASK_TOML)
    patched_copies: list[Path] = []
    # Hold the TemporaryDirectory objects alive. Without a strong reference,
    # CPython's refcounting fires their finalizer the moment this function
    # returns, which removes the tree with a ResourceWarning and hides whether
    # the explicit cleanup ever ran.
    live_tmpdirs: list[tempfile.TemporaryDirectory] = []
    real_tmpdir = tempfile.TemporaryDirectory

    def _keep(*args, **kwargs):
        created = real_tmpdir(*args, **kwargs)
        live_tmpdirs.append(created)
        return created

    def _record(task_dir, _hc):
        patched_copies.append(Path(task_dir))

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _keep)
    monkeypatch.setattr(harbor_ephemeral, "_patch_task_toml", _record)

    outcome = await run_ephemeral_harbor_trial(
        task_path=task_path,
        agent="claude-code",
        jobs_dir=tmp_path / "jobs",
        model="geometric/glm-0.0-unserved",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        harbor_config={**_EPHEMERAL_HC, "docker_image": "example.invalid/img:1"},
    )

    assert outcome.exception_type == "ValueError"
    assert patched_copies, "the task tree should have been copied for patching"
    assert not patched_copies[0].parent.exists()
    assert "Geometric serves only" in (outcome.error or "")


def test_ephemeral_hdo_trial_blanks_bedrock_without_an_ambient_platform_key(
    monkeypatch,
):
    """The HDO route selects its own key and clears competing Bedrock selectors."""
    monkeypatch.setattr(harbor_ephemeral.settings, "anthropic_hdo_api_key", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_HDO_API_KEY", "hdo-secret")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-token")

    payload = _compat_payload("anthropic-hdo/claude-opus-4-5")

    assert payload["runtime_env"]["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert payload["runtime_env"]["AWS_BEARER_TOKEN_BEDROCK"] == ""


def test_child_merges_worker_env_over_shaped_compat_env(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    payload = _compat_payload("fireworks/glm-5p2")
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": payload["model"],
            "environment_config": {},
            "agent_config": payload["agent_config"],
            "verifier": {},
            "artifacts": [],
            "runtime_env": payload["runtime_env"],
            "extra_agent_env": {"ANTHROPIC_AUTH_TOKEN": "byok-token"},
        }
    )

    env = config.agents[0].env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
    assert env["ANTHROPIC_MODEL"] == "accounts/fireworks/models/glm-5p2"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "byok-token"


def test_child_applies_submitted_agent_config(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "mini-swe-agent",
            "model": "openrouter/tencent/hy3",
            "environment": "modal",
            "environment_config": {},
            "agent_config": {
                "env": {"UV_HTTP_RETRIES": "8"},
                "extra_allowed_hosts": ["astral.sh", "pypi.org"],
                "override_setup_timeout_sec": 1800,
            },
            "verifier": {},
            "artifacts": [],
        }
    )
    agent_config = config.agents[0]
    assert agent_config.name is None
    assert agent_config.import_path == "oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent"
    assert agent_config.model_name == "openrouter/tencent/hy3"
    assert agent_config.env == {"UV_HTTP_RETRIES": "8"}
    assert agent_config.extra_allowed_hosts == ["astral.sh", "pypi.org"]
    assert agent_config.override_setup_timeout_sec == 1800


def test_child_preserves_submitted_agent_import_path(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "ignored-built-in-name",
            "model": "custom/model",
            "environment": "docker",
            "environment_config": {},
            "agent_config": {"import_path": "custom.module:CustomAgent"},
            "verifier": {},
            "artifacts": [],
        }
    )
    assert config.agents[0].name is None
    assert config.agents[0].import_path == "custom.module:CustomAgent"
    assert config.agents[0].model_name == "custom/model"


def test_child_applies_extra_agent_env_to_agent_config(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": "claude-sonnet-4-5",
            "environment": "docker",
            "environment_config": {},
            "verifier": {},
            "artifacts": [],
            "extra_agent_env": {"ODDISH_API_KEY": "secret-mint"},
        }
    )
    assert config.agents[0].env.get("ODDISH_API_KEY") == "secret-mint"


def test_child_merges_worker_env_over_submitted_env(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "mini-swe-agent",
            "model": "openrouter/tencent/hy3",
            "environment": "modal",
            "environment_config": {},
            "agent_config": {
                "env": {
                    "UV_HTTP_RETRIES": "8",
                    "OPENAI_BASE_URL": "https://stale.invalid",
                    "ODDISH_API_KEY": "stale",
                }
            },
            "verifier": {},
            "artifacts": [],
            "runtime_env": {"OPENAI_BASE_URL": "https://worker.example/v1"},
            "extra_agent_env": {"ODDISH_API_KEY": "secret-mint"},
        }
    )
    assert config.agents[0].env == {
        "UV_HTTP_RETRIES": "8",
        "OPENAI_BASE_URL": "https://worker.example/v1",
        "ODDISH_API_KEY": "secret-mint",
    }


_SOURCE = "https://github.com/dot-agi/harbor"
_SHA = "a" * 40


def test_child_default_agent_not_routed_through_subclass(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": "claude-sonnet-4-5",
            "environment": "docker",
            "environment_config": {},
            "verifier": {},
            "artifacts": [],
        }
    )
    ac = config.agents[0]
    assert ac.name == "claude-code"
    assert ac.import_path is None


def test_read_outcome_with_result_json_uses_extractor(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    result_path.write_text("{}")
    (tmp_path / "outcome.json").write_text(
        json.dumps({"job_dir": str(tmp_path), "job_result_path": str(result_path)})
    )
    sentinel = HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=3.0,
        job_result_path=result_path,
        job_dir=tmp_path,
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_extract_outcome_from_job_result", lambda **k: sentinel
    )

    class _FakeJobResult:
        @staticmethod
        def model_validate_json(_text):
            return object()

    import harbor.models.job.result as result_mod

    monkeypatch.setattr(result_mod, "JobResult", _FakeJobResult)
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=0,
        duration=3.0,
        stderr="",
        stdout_tail="",
    )
    assert outcome is sentinel


def test_harbor_override_import_error_is_non_retryable():
    from oddish.workers.queue.trial_handler import (
        _NON_HARBOR_RETRYABLE_EXCEPTION_TYPES,
    )

    assert HarborOverrideImportError.__name__ in _NON_HARBOR_RETRYABLE_EXCEPTION_TYPES


def test_spawn_args_requests_daytona_extra_for_daytona_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.DAYTONA
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["daytona"])
    assert req.startswith("harbor[daytona] @ git+")


def test_spawn_args_requests_archil_extra_for_archil_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.ARCHIL
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["archil"])
    assert req.startswith("harbor[archil] @ git+")


def test_spawn_args_requests_thunder_extra_for_thunder_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.THUNDER
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["thunder"])
    assert req.startswith("harbor[thunder] @ git+")


def test_ephemeral_extra_map_allows_harbor_without_thunder():
    class PublicHarborEnvironmentType:
        DAYTONA = object()
        ARCHIL = object()
        MODAL = object()
        E2B = object()
        RUNLOOP = object()
        GKE = object()
        NOVITA = object()
        TENSORLAKE = object()
        CWSANDBOX = object()
        WANDB = object()
        ISLO = object()
        EC2 = object()

    extras = harbor_ephemeral._environment_harbor_extras(PublicHarborEnvironmentType)

    assert "thunder" not in extras.values()


def test_spawn_args_requests_ec2_extra_for_ec2_env():
    args = harbor_ephemeral._spawn_args(_SOURCE, _SHA, environment=EnvironmentType.EC2)
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["ec2"])


def test_payload_serializes_resolved_environment_config_without_child_merges():
    resolved = EnvironmentConfig.model_validate(
        {
            "type": "modal",
            "kwargs": {
                "keep": "value",
                "auto_stop_interval_mins": 17,
                "auto_delete_interval_mins": 23,
                "ephemeral": False,
            },
        }
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=resolved,
        raw_harbor_config={},
    )

    assert payload["environment_config"] == resolved.model_dump(mode="json")
    assert "daytona_kwargs" not in payload
    child_config = _build_job_config(
        {
            **payload,
            "daytona_kwargs": {"auto_stop_interval_mins": 999},
        }
    )
    assert child_config.environment.model_dump(mode="json") == (
        resolved.model_dump(mode="json")
    )


def test_spawn_args_no_extra_for_docker_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.DOCKER
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA)
    assert "[" not in req.split("@", 1)[0]


def test_spawn_args_defaults_to_docker_no_extra():
    args = harbor_ephemeral._spawn_args(_SOURCE, _SHA)
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA)


def test_child_process_env_exposes_existing_parent_site_packages(monkeypatch, tmp_path):
    first = tmp_path / "site-a"
    second = tmp_path / "site-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        harbor_ephemeral.site,
        "getsitepackages",
        lambda: [str(first), str(tmp_path / "missing"), str(second)],
    )

    env = harbor_ephemeral._child_process_env()

    assert env["ODDISH_PARENT_SITE_PACKAGES"].split(os.pathsep) == [
        str(first.resolve()),
        str(second.resolve()),
    ]


def test_child_process_env_fails_loudly_without_parent_site_packages(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        harbor_ephemeral.site,
        "getsitepackages",
        lambda: [str(tmp_path / "missing")],
    )

    with pytest.raises(
        HarborOverrideImportError,
        match="cannot locate parent site-packages",
    ):
        harbor_ephemeral._child_process_env()


_FAKE_CHILD = textwrap.dedent(
    """
    import json, sys
    payload = json.loads(open(sys.argv[1]).read())
    sentinel = "_oddish_harbor_event"
    for ev in ("start", "agent-start"):
        print(json.dumps({sentinel: True, "event": ev, "trial_id": payload.get("trial_id")}), flush=True)
    # Helm setup can emit one JSON/log line larger than asyncio's 64 KiB
    # default StreamReader limit. The parent must preserve the event stream.
    print("harbor: " + "x" * (128 * 1024), flush=True)
    print(json.dumps({sentinel: True, "event": "end", "trial_id": payload.get("trial_id"),
                      "result": {"verifier_result": {"rewards": {"reward": 1.0}}}}), flush=True)
    open(payload["outcome_path"], "w").write(json.dumps(
        {"job_dir": payload["jobs_dir"], "job_result_path": None,
        "duration_sec": 0.1, "error": "fake child: no result", "exception_type": None}))
    """
)


@pytest.mark.asyncio
async def test_run_ephemeral_streams_events_and_reads_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda p: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "fake_child.py"
    child.write_text(_FAKE_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda s, sha, **kw: [sys.executable, str(child)],
    )

    seen: list[str] = []

    async def hook(event):
        seen.append(event.event.value)

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    outcome = await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="claude-code",
        jobs_dir=tmp_path / "jobs",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        model="claude-sonnet-4-5",
        hook_callback=hook,
        trial_id="t-eph",
        harbor_config=_EPHEMERAL_HC,
    )

    assert seen == ["start", "agent-start", "end"]
    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.job_dir is not None


_SLEEP_CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    payload = json.loads(open(sys.argv[1]).read())
    open(payload["jobs_dir"] + "/pid", "w").write(str(os.getpid()))
    time.sleep(120)
    """
)


_ENV_CHILD = textwrap.dedent(
    """
    import json, os, stat, sys
    payload = json.loads(open(sys.argv[1]).read())
    key_path = payload["environment_config"]["kwargs"]["ssh_key_path"]
    profile_path = os.environ["AWS_SHARED_CREDENTIALS_FILE"]
    open(payload["jobs_dir"] + "/child-env.json", "w").write(json.dumps({
        "secret": os.environ.get("ODDISH_EC2_SSH_PRIVATE_KEY"),
        "access": os.environ.get("ODDISH_EC2_AWS_ACCESS_KEY_ID"),
        "aws_secret": os.environ.get("ODDISH_EC2_AWS_SECRET_ACCESS_KEY"),
        "token": os.environ.get("ODDISH_EC2_AWS_SESSION_TOKEN"),
        "key_path": key_path,
        "key_mode": stat.S_IMODE(os.stat(key_path).st_mode),
        "profile": payload["environment_config"]["kwargs"]["aws_profile"],
        "profile_mode": stat.S_IMODE(os.stat(profile_path).st_mode),
    }))
    open(payload["outcome_path"], "w").write(json.dumps({
        "job_dir": payload["jobs_dir"],
        "job_result_path": None,
        "duration_sec": 0.1,
        "error": "fake child: no result",
        "exception_type": None,
    }))
    """
)


_FAILED_CHILD = textwrap.dedent(
    """
    import json, stat, sys
    from pathlib import Path

    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text())
    Path(payload["jobs_dir"], "payload-observation.json").write_text(json.dumps({
        "payload_path": str(payload_path),
        "payload_mode": stat.S_IMODE(payload_path.stat().st_mode),
    }))
    raise SystemExit(19)
    """
)


def test_child_unlinks_payload_even_when_json_is_invalid(tmp_path):
    payload_path = tmp_path / "invalid-payload.json"
    payload_path.write_text("not json")

    with pytest.raises(json.JSONDecodeError):
        _read_payload_and_unlink(payload_path)

    assert not payload_path.exists()


@pytest.mark.asyncio
async def test_failed_ephemeral_child_cannot_expose_payload_in_job_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda _path: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *_a, **_k: None
    )
    child = tmp_path / "failed_child.py"
    child.write_text(_FAILED_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda _source, _sha, **_kwargs: [sys.executable, str(child)],
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    secret = "must-never-become-an-artifact"

    outcome = await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="nop",
        jobs_dir=jobs_dir,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        harbor_config=_EPHEMERAL_HC,
        extra_agent_env={"PROVIDER_SECRET": secret},
    )

    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.job_dir is not None
    observation = json.loads((outcome.job_dir / "payload-observation.json").read_text())
    payload_path = Path(observation["payload_path"])
    assert payload_path.parent != outcome.job_dir
    assert observation["payload_mode"] == 0o600
    assert not payload_path.exists()
    artifact_contents = "\n".join(
        path.read_text(errors="replace")
        for path in outcome.job_dir.rglob("*")
        if path.is_file()
    )
    assert secret not in artifact_contents


@pytest.mark.asyncio
async def test_ephemeral_child_uses_key_path_without_inheriting_private_key_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda _path: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *_a, **_k: None
    )
    child = tmp_path / "env_child.py"
    child.write_text(_ENV_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda _source, _sha, **_kwargs: [sys.executable, str(child)],
    )
    monkeypatch.setenv("ODDISH_EC2_SSH_PRIVATE_KEY", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_ACCESS_KEY_ID", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_SECRET_ACCESS_KEY", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_SESSION_TOKEN", "must-not-reach-child")
    key_path = tmp_path / "ec2-key"
    key_path.write_text("private key\n")
    key_path.chmod(0o600)
    profile_path = tmp_path / "aws-credentials"
    profile_path.write_text("[oddish-ec2]\n")
    profile_path.chmod(0o600)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(profile_path))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    resolved = EnvironmentConfig.model_validate(
        {
            "type": "ec2",
            "kwargs": {
                "ssh_key_path": str(key_path),
                "aws_profile": "oddish-ec2",
            },
        }
    )

    await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="nop",
        jobs_dir=jobs_dir,
        environment_config=resolved,
        harbor_config=_EPHEMERAL_HC,
    )

    child_state = json.loads((next(jobs_dir.iterdir()) / "child-env.json").read_text())
    assert child_state == {
        "secret": None,
        "access": None,
        "aws_secret": None,
        "token": None,
        "key_path": str(key_path),
        "key_mode": 0o600,
        "profile": "oddish-ec2",
        "profile_mode": 0o600,
    }


@pytest.mark.asyncio
async def test_run_ephemeral_cancel_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda p: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "sleep_child.py"
    child.write_text(_SLEEP_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda s, sha, **kw: [sys.executable, str(child)],
    )
    jobs_dir = tmp_path / "jobs"
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    task = asyncio.create_task(
        run_ephemeral_harbor_trial(
            task_path=task_dir,
            agent="nop",
            jobs_dir=jobs_dir,
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            trial_id="t-cancel",
            harbor_config=_EPHEMERAL_HC,
        )
    )
    pid_file = jobs_dir / "task.nop.t-cancel" / "pid"
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "child never started"
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_upload_probe_assets_splits_targets(tmp_path):
    from oddish.workers.queue.trial_handler import _upload_probe_assets
    from oddish.worker.probe_staging import PROBE_HARNESS_DIR

    assets = tmp_path / "task"
    (assets / "solution").mkdir(parents=True)
    (assets / "solution" / "f.txt").write_text("x")

    uploads = []

    class FakeEnv:
        async def upload_dir(self, *, source_dir, target_dir):
            names = sorted(p.name for p in Path(source_dir).iterdir())
            uploads.append((target_dir, names))

    await _upload_probe_assets(FakeEnv(), assets, "t1")

    targets = {t for t, _ in uploads}
    # Only the CLI is uploaded; the STAGE_DIR asset push was dropped (Task 8).
    assert PROBE_HARNESS_DIR in targets
    # CLI mount carries ONLY the CLI.
    harness = next(names for t, names in uploads if t == PROBE_HARNESS_DIR)
    assert harness == ["oddish-query"]


@pytest.mark.asyncio
async def test_upload_probe_assets_fails_when_qa_submission_contract_is_missing(
    tmp_path,
):
    from oddish.workers.queue.trial_handler import _upload_probe_assets

    assets = tmp_path / "analysis-task"
    assets.mkdir()
    (assets / "submit-analysis-result").write_text("#!/bin/sh\n")
    (assets / ".analysis-contract").mkdir()

    class FailingEnv:
        async def upload_dir(self, *, source_dir, target_dir):
            raise OSError("sandbox upload unavailable")

    with pytest.raises(RuntimeError, match="required QA submission contract"):
        await _upload_probe_assets(FailingEnv(), assets, "qa-1")


@pytest.mark.parametrize("api_model", ["claude-opus-5", "claude-opus-5-5"])
def test_build_payload_normalizes_claude_model_when_bedrock_is_blanked(
    monkeypatch, api_model
):
    """An explicit Anthropic child carries the API ID and clears Bedrock mode."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(harbor_ephemeral.settings, "claude_code_force_direct_api", True)
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model=f"anthropic/{api_model}",
        environment=EnvironmentType.DOCKER,
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    assert payload["runtime_env"]["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert payload["model"] == api_model


@pytest.mark.parametrize(
    "force_direct,ambient_key", [(False, "sk-ant-test"), (True, None)]
)
@pytest.mark.parametrize("api_model", ["claude-opus-5", "claude-opus-5-5"])
def test_build_payload_keeps_bedrock_model_when_bedrock_stays_on(
    monkeypatch, force_direct, ambient_key, api_model
):
    """With Bedrock routing left in place the stored Bedrock id is correct."""
    if ambient_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        harbor_ephemeral.settings, "claude_code_force_direct_api", force_direct
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model=f"global.anthropic.{api_model}",
        environment=EnvironmentType.DOCKER,
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    assert payload["runtime_env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert payload["model"] == f"global.anthropic.{api_model}"


def test_build_payload_leaves_non_claude_agents_alone(monkeypatch):
    """Only claude-code is rerouted, so other agents keep their submitted id."""
    monkeypatch.setattr(harbor_ephemeral.settings, "claude_code_force_direct_api", True)
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="mini-swe-agent",
        model="openrouter/tencent/hy3",
        environment=EnvironmentType.MODAL,
        raw_harbor_config=dict(_EPHEMERAL_HC),
        extra_agent_env={"ANTHROPIC_API_KEY": "test-user-key"},
    )

    assert payload["runtime_env"] == {}
    assert payload["model"] == "openrouter/tencent/hy3"


@pytest.mark.parametrize("api_model", ["claude-opus-5", "claude-opus-5-5"])
@pytest.mark.parametrize("route", ["anthropic", "bedrock"])
def test_dispatch_paths_agree_on_the_claude_model_id(monkeypatch, api_model, route):
    """Both dispatch paths must hand Claude Code the same model id.

    The in-process path normalizes in ``_build_agent_config`` and the ephemeral
    path in ``_build_payload``. Which one ran a trial is an Oddish scheduling
    detail, so it must not change the model id that reaches the provider.
    """
    from oddish.workers.harbor.agent_config import _build_agent_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(harbor_ephemeral.settings, "claude_code_force_direct_api", True)
    agent = "claude-code"
    model = (
        f"anthropic/{api_model}"
        if route == "anthropic"
        else f"global.anthropic.{api_model}"
    )
    expected = api_model if route == "anthropic" else f"global.anthropic.{api_model}"

    in_process = _build_agent_config(
        agent=agent,
        model=model,
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent=agent,
        model=model,
        environment=EnvironmentType.DOCKER,
        raw_harbor_config=dict(_EPHEMERAL_HC),
    )

    assert payload["model"] == in_process.model_name == expected


@pytest.mark.parametrize("force_direct", [True, False])
def test_payload_bedrock_route_ignores_trial_anthropic_key(monkeypatch, force_direct):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-token")
    monkeypatch.setattr(
        harbor_ephemeral.settings, "claude_code_force_direct_api", force_direct
    )
    agent_env = {
        "ANTHROPIC_API_KEY": "test-user-key",
        "ODDISH_API_KEY": "test-read-key",
    }
    before = dict(os.environ)
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="global.anthropic.claude-opus-5",
        environment=EnvironmentType.DOCKER,
        raw_harbor_config=dict(_EPHEMERAL_HC),
        extra_agent_env=agent_env,
    )
    child_agent = _build_job_config(payload).agents[0]

    assert os.environ == before
    assert agent_env == {
        "ANTHROPIC_API_KEY": "test-user-key",
        "ODDISH_API_KEY": "test-read-key",
    }
    assert child_agent.env["ANTHROPIC_API_KEY"] == ""
    assert child_agent.env["ODDISH_API_KEY"] == "test-read-key"
    assert child_agent.model_name == "global.anthropic.claude-opus-5"
    assert child_agent.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert payload["runtime_env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert payload["runtime_env"]["ANTHROPIC_API_KEY"] == ""


@pytest.mark.parametrize("force_direct", [True, False])
@pytest.mark.parametrize("hdo_key", ["test-hdo-key", ""])
@pytest.mark.parametrize("worker_key", ["test-worker-key", None])
@pytest.mark.parametrize(
    "environment", [EnvironmentType.DOCKER, EnvironmentType.THUNDER]
)
def test_payload_hdo_key_wins_over_worker_and_user_keys(
    tmp_path, monkeypatch, force_direct, hdo_key, worker_key, environment
):
    from oddish.config import BEDROCK_ENV_VARS

    if worker_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", worker_key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-token")
    monkeypatch.delenv("ANTHROPIC_HDO_API_KEY", raising=False)
    monkeypatch.setattr(harbor_ephemeral.settings, "anthropic_hdo_api_key", hdo_key)
    monkeypatch.setattr(
        harbor_ephemeral.settings, "claude_code_force_direct_api", force_direct
    )
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text(
        '[environment]\nnetwork_mode = "public"\n'
        '[agent]\nnetwork_mode = "allowlist"\n'
        '[verifier]\nnetwork_mode = "no-network"\n'
    )
    before = dict(os.environ)
    payload = _build_payload(
        task_path=task_path,
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="anthropic-hdo/claude-opus-5",
        environment=environment,
        raw_harbor_config=dict(_EPHEMERAL_HC),
        extra_agent_env={"ANTHROPIC_API_KEY": "test-user-key"},
    )
    child_agent = _build_job_config(payload).agents[0]

    assert os.environ == before
    assert child_agent.model_name == "claude-opus-5"
    assert payload["runtime_env"]["ANTHROPIC_API_KEY"] == hdo_key
    assert child_agent.env["ANTHROPIC_API_KEY"] == hdo_key
    assert all(payload["runtime_env"][name] == "" for name in BEDROCK_ENV_VARS)
    assert all(child_agent.env[name] == "" for name in BEDROCK_ENV_VARS)
    if environment == EnvironmentType.THUNDER:
        assert "api.anthropic.com" in child_agent.extra_allowed_hosts


def test_payload_declares_gateway_hosts_for_pause_draining(monkeypatch):
    for key in (*KNOWN_TRANSPORT_BASE_URL_KEYS, *TBH_BASE_URL_KEYS):
        monkeypatch.delenv(key, raising=False)
    resolved = EnvironmentConfig(
        type=EnvironmentType.ARCHIL, kwargs={"drain_on_pause": True}
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment_config=resolved,
        raw_harbor_config={},
        extra_agent_env={"ANTHROPIC_BASE_URL": "https://gateway.test/anthropic"},
    )

    assert payload["environment_config"]["kwargs"]["drain_on_pause_hosts"] == [
        "gateway.test"
    ]
    assert "drain_on_pause_hosts" not in resolved.kwargs


@pytest.mark.parametrize("agent", ["muse-code", "tbh"])
@pytest.mark.parametrize("enabled", [True, False])
def test_payload_declares_pause_drain_kwargs_base_url(monkeypatch, agent, enabled):
    for key in (*KNOWN_TRANSPORT_BASE_URL_KEYS, *TBH_BASE_URL_KEYS):
        monkeypatch.delenv(key, raising=False)
    resolved = EnvironmentConfig(
        type=EnvironmentType.ARCHIL,
        kwargs={"drain_on_pause": enabled, "drain_on_pause_hosts": ["manual.test"]},
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent=agent,
        model="meta/muse-spark-1.2",
        environment_config=resolved,
        raw_harbor_config={
            "agent_config": {"kwargs": {"base_url": "https://kwargs-gateway.test/v1"}}
        },
    )

    hosts = payload["environment_config"]["kwargs"]["drain_on_pause_hosts"]
    assert "manual.test" in hosts
    assert ("kwargs-gateway.test" in hosts) is enabled
    assert resolved.kwargs["drain_on_pause_hosts"] == ["manual.test"]
