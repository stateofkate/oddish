"""The run's private-registry login reaches the sandbox's Docker."""

from __future__ import annotations

import base64
import json
import stat

import pytest

from oddish.mon import env, registry_login

_BASE = {env.API_URL_VAR: "https://api.example", env.TOKEN_VAR: "j.1.t"}
_LOGIN = [{"registry": "ghcr.io", "username": "bot", "token": "s3cret"}]


def test_login_is_read_once_and_removed_from_the_environment():
    environ = {**_BASE, env.REGISTRY_AUTH_VAR: json.dumps(_LOGIN)}
    loaded = env.load(environ)
    assert loaded.registry_auth == _LOGIN
    assert env.REGISTRY_AUTH_VAR not in environ
    assert "s3cret" not in repr(loaded)


def test_no_login_is_the_default():
    assert env.load(dict(_BASE)).registry_auth == []


@pytest.mark.parametrize("bad", ["not json", json.dumps({"registry": "ghcr.io"})])
def test_malformed_login_is_refused(bad):
    with pytest.raises(env.MonitorEnvError):
        env.load({**_BASE, env.REGISTRY_AUTH_VAR: bad})


def test_docker_config_is_written_private_to_the_monitor(tmp_path):
    registries = registry_login.login(_LOGIN, home=tmp_path)

    config = tmp_path / ".docker" / "config.json"
    assert registries == ["ghcr.io"]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    auth = json.loads(config.read_text())["auths"]["ghcr.io"]["auth"]
    assert base64.b64decode(auth).decode() == "bot:s3cret"


def test_no_login_writes_nothing(tmp_path):
    assert registry_login.login([], home=tmp_path) == []
    assert not (tmp_path / ".docker").exists()
