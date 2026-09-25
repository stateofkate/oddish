from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import modal_app
import pytest
import thunder_readiness
import worker.functions as worker_functions


def test_disabled_deploy_has_no_thunder_secret_dependency() -> None:
    assert modal_app.THUNDER_SECRET_PLAN == []
    assert modal_app.thunder_worker_secrets == []


def test_thunder_public_image_env_excludes_credentials() -> None:
    assert modal_app._THUNDER_PUBLIC_ENV_NAMES == {
        "ODDISH_THUNDER_CAPACITY_FALLBACK",
        "ODDISH_THUNDER_ENABLED",
        "ODDISH_THUNDER_FALLBACK_PROVIDER",
        "ODDISH_THUNDER_MAX_CAPACITY",
        "ODDISH_THUNDER_MAX_FAILED_ATTEMPTS",
        "ODDISH_THUNDER_SECRET_NAME",
    }
    assert "TNR_API_URL" not in modal_app.ENV_VARS
    assert "TNR_API_TOKEN" not in modal_app.ENV_VARS


def test_enabled_import_builds_one_worker_only_secret() -> None:
    code = """
import json
import modal_app
import worker.functions as worker_functions
print(json.dumps({
    "plan": modal_app.THUNDER_SECRET_PLAN,
    "worker_count": len(modal_app.thunder_worker_secrets),
    "base_overlap": any(
        secret in modal_app.runtime_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "thunder_lane_has_secret": all(
        secret in worker_functions.thunder_trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "generic_has_secret": any(
        secret in worker_functions.trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "capacity": modal_app.ENV_VARS["ODDISH_THUNDER_MAX_CAPACITY"],
    "capacity_fallback": modal_app.ENV_VARS["ODDISH_THUNDER_CAPACITY_FALLBACK"],
    "fallback_provider": modal_app.ENV_VARS["ODDISH_THUNDER_FALLBACK_PROVIDER"],
    "max_failed_attempts": modal_app.ENV_VARS["ODDISH_THUNDER_MAX_FAILED_ATTEMPTS"],
}))
"""
    env = {
        **os.environ,
        "ODDISH_THUNDER_ENABLED": "true",
        "ODDISH_THUNDER_SECRET_NAME": "test-thunder",
        "ODDISH_THUNDER_CAPACITY_FALLBACK": "T",
        "ODDISH_THUNDER_FALLBACK_PROVIDER": "modal",
        "ODDISH_SAURON_AWS_SECRET_NAME": "",
    }
    env.pop("ODDISH_THUNDER_MAX_CAPACITY", None)
    env.pop("ODDISH_THUNDER_MAX_FAILED_ATTEMPTS", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(modal_app.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "plan": ["test-thunder"],
        "worker_count": 1,
        "base_overlap": False,
        "thunder_lane_has_secret": True,
        "generic_has_secret": False,
        "capacity": "128",
        "capacity_fallback": "true",
        "fallback_provider": "modal",
        "max_failed_attempts": "2",
    }


def test_only_thunder_lane_workers_receive_thunder_secret() -> None:
    assert worker_functions.trial_worker_secrets == [*modal_app.runtime_secrets]
    assert worker_functions.thunder_trial_worker_secrets == [
        *modal_app.runtime_secrets,
        *modal_app.thunder_worker_secrets,
    ]
    assert all(
        secret not in worker_functions.ec2_trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    )
    assert all(
        secret not in worker_functions.reconciler_secrets
        for secret in modal_app.thunder_worker_secrets
    )


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_deploy_settings_survive_shared_runtime_secret_overrides(enabled) -> None:
    code = """
import importlib
import json
import os
import modal

captured = []
original = modal.Secret.from_dict
def capture(values, *args, **kwargs):
    captured.append(dict(values))
    return original(values, *args, **kwargs)
modal.Secret.from_dict = capture

import modal_app
authoritative = captured[-1]
plan = modal_app.THUNDER_SECRET_PLAN
dependencies = len(modal_app.runtime_secrets) + len(modal_app.thunder_worker_secrets)
expected = {name: modal_app.ENV_VARS[name] for name in modal_app._THUNDER_PUBLIC_ENV_NAMES}
assert all(authoritative[name] == value for name, value in expected.items())

# Modal injects shared secrets over image env. The final deploy-owned
# secret must restore this app's settings before container imports run.
os.environ.update({
    "ODDISH_THUNDER_ENABLED": "false" if expected["ODDISH_THUNDER_ENABLED"] == "true" else "true",
    "ODDISH_THUNDER_SECRET_NAME": "old-shared-thunder",
    "ODDISH_THUNDER_CAPACITY_FALLBACK": "true",
    "ODDISH_THUNDER_MAX_CAPACITY": "999",
})
os.environ.update(authoritative)
importlib.reload(modal_app)
assert modal_app.THUNDER_SECRET_PLAN == plan
assert len(modal_app.runtime_secrets) + len(modal_app.thunder_worker_secrets) == dependencies
from oddish.config import Settings
settings = Settings(_env_file=None)
assert settings.thunder_enabled == (expected["ODDISH_THUNDER_ENABLED"] == "true")
assert settings.thunder_capacity_fallback is False
assert settings.thunder_max_capacity == 64
print("runtime Thunder settings preserved")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(modal_app.__file__).parent,
        env={
            **os.environ,
            "ODDISH_THUNDER_ENABLED": enabled,
            "ODDISH_THUNDER_SECRET_NAME": "test-thunder",
            "ODDISH_THUNDER_CAPACITY_FALLBACK": "false",
            "ODDISH_THUNDER_MAX_CAPACITY": "64",
            "ODDISH_SAURON_AWS_SECRET_NAME": "",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == "runtime Thunder settings preserved"


def test_worker_readiness_validates_dependencies_without_returning_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TNR_API_URL", "https://thunder.invalid")
    monkeypatch.setenv("TNR_API_TOKEN", "do-not-return")
    monkeypatch.setitem(sys.modules, "asyncssh", ModuleType("asyncssh"))
    versions = {
        "thunder-sandbox": "0.7.5",
        "aiohttp": "3.12.0",
        "asyncssh": "2.21.0",
        "cryptography": "45.0.0",
    }
    monkeypatch.setattr(thunder_readiness, "version", versions.__getitem__)

    result = thunder_readiness.check_thunder_worker.get_raw_f()()

    assert result["thunder_sandbox"] == "0.7.5"
    assert result["api_url_resolved"] is True
    assert result["api_token_resolved"] is True
    assert "https://thunder.invalid" not in result.values()
    assert "do-not-return" not in result.values()
