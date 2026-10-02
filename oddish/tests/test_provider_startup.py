"""Exercise launcher serialization and the real monitor import order in a fresh process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from oddish.workers.launcher import launch_env

_STARTUP = """
import builtins, json, os
from oddish.mon import __main__, env
real_import = builtins.__import__
real_load = env.load
loads = []
def load():
    loads.append(1)
    return real_load()
env.load = load
def capture_import(name, *args, **kwargs):
    module = real_import(name, *args, **kwargs)
    if name == 'oddish.mon.runner':
        from oddish.config import settings
        async def run(monitor_env):
            print('STARTUP=' + json.dumps({
                'settings': {name: getattr(settings, name) for name in json.loads(os.environ['CHECK_FIELDS'])},
                'agent': monitor_env.agent_env,
                'driver': monitor_env.driver_env,
                'task': monitor_env.task_env,
                'loads': len(loads),
                'token_present': 'ODDISH_MON_TOKEN' in os.environ,
                'release': os.environ.get('ODDISH_RELEASE'),
            }))
            return 0
        module.run = run
    return module
builtins.__import__ = capture_import
raise SystemExit(__main__.main(['run']))
"""


@pytest.mark.parametrize(
    "model,fields,ambient",
    [
        ("meta/test", {"meta_base_url": "https://configured.example/v1"}, {}),
        (
            "geometric/test",
            {
                "geometric_base_url": "https://configured.example/v1",
                "geometric_anthropic_base_url": "https://anthropic.example",
            },
            {},
        ),
        ("hunyuan/test", {"hunyuan_base_url": "https://configured.example/v1"}, {}),
        (
            "openrouter/test",
            {},
            {
                "OPENROUTER_BASE_URL": "https://configured.example/v1",
                "OPENROUTER_API_KEY": "agent-key",
            },
        ),
        (
            "fireworks/test",
            {},
            {
                "FIREWORKS_BASE_URL": "https://configured.example/v1",
                "FIREWORKS_API_KEY": "agent-key",
            },
        ),
        ("anthropic-hdo/claude-sonnet-5-5", {"anthropic_hdo_api_key": "hdo-key"}, {}),
        ("anthropic/claude-sonnet-5-5", {"anthropic_api_key": "user-key"}, {}),
        (
            "vertex_ai/gemini-3.8-flash",
            {"vertex_ai_project_id": "test-project", "vertex_ai_location": "us-east5"},
            {},
        ),
        (
            "openai/gpt-5.2",
            {
                "openai_provider": "azure",
                "azure_openai_deployments": {"openai/gpt-5.2": "custom-deployment"},
            },
            {},
        ),
    ],
)
def test_fresh_monitor_preserves_route_and_separates_judge(
    monkeypatch, tmp_path, model, fields, ambient
):
    platform = launch_env.settings
    source_fields = dict(fields)
    if model.startswith("anthropic/"):
        source_fields["anthropic_api_key"] = "platform-key"
    for name, value in source_fields.items():
        monkeypatch.setattr(platform, name, value)
    for name in (
        "anthropic_hdo_api_key",
        "meta_api_key",
        "geometric_api_key",
        "hunyuan_api_key",
    ):
        if name not in source_fields:
            monkeypatch.setattr(platform, name, "agent-key")
    for name, value in [
        ("vertex_ai_api_key", "express-key"),
        ("azure_openai_api_key", "azure-key"),
        ("azure_openai_endpoint", "https://azure.example/openai/v1"),
        ("azure_openai_api_version", "2025-01-01"),
    ]:
        monkeypatch.setattr(
            platform, name, SecretStr(value) if name == "vertex_ai_api_key" else value
        )
    monkeypatch.setattr(
        platform,
        "vertex_ai_credentials_json",
        SecretStr('{"type":"service_account","project_id":"test-project"}'),
    )
    prepared = SimpleNamespace(
        trial_agent="claude-code",
        trial_model=None,
        trial_harbor_config={
            "agent_config": {"name": "claude-code", "model_name": model}
        },
    )
    sandbox = launch_env.build_sandbox_env(
        prepared,
        api_url="https://api.test",
        credential="test.1.token",
        byok_env={"ANTHROPIC_API_KEY": "user-key"},
        judge_env={"ANTHROPIC_API_KEY": "judge-key"},
        ambient={**ambient, "ODDISH_RELEASE": "test-commit"},
    )
    process = subprocess.run(
        [sys.executable, "-c", _STARTUP],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join(sys.path),
            "ODDISH_MON_LOG_PATH": str(tmp_path / "monitor.log"),
            "CHECK_FIELDS": json.dumps(list(fields)),
            **sandbox,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    observed = json.loads(
        next(
            line.removeprefix("STARTUP=")
            for line in process.stdout.splitlines()
            if line.startswith("STARTUP=")
        )
    )
    assert observed["settings"] == fields
    assert observed["task"] == {"ANTHROPIC_API_KEY": "judge-key"}
    for name, value in ambient.items():
        assert observed["driver"][name] == value
    assert "judge-key" not in observed["agent"].values()
    assert observed["loads"] == 1
    assert observed["token_present"] is False
    assert observed["release"] == "test-commit"
    assert not any(
        name.startswith(("ODDISH_", "VERTEX_AI_")) for name in observed["agent"]
    )
