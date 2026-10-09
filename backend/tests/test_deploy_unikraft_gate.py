"""Deploy-time Unikraft default gate (modal_app.assert_unikraft_default_enabled).

ODDISH_HARBOR_ENVIRONMENT=unikraft routes QA and audit trials to Unikraft.
These tests pin the gate's contract: that default without the registered
backend blocks the deploy; every other combination never does.
"""

import pytest

import modal_app


def _configure(monkeypatch, **env):
    for key in ("ODDISH_HARBOR_ENVIRONMENT", "ODDISH_UNIKRAFT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(modal_app, "LOCAL_DOTENV_VARS", {})


def test_no_harbor_environment_is_a_noop(monkeypatch):
    _configure(monkeypatch)
    modal_app.assert_unikraft_default_enabled()


def test_modal_default_is_a_noop_without_unikraft(monkeypatch):
    _configure(monkeypatch, ODDISH_HARBOR_ENVIRONMENT="modal")
    modal_app.assert_unikraft_default_enabled()


def test_unikraft_default_without_the_backend_blocks_the_deploy(monkeypatch):
    _configure(monkeypatch, ODDISH_HARBOR_ENVIRONMENT="unikraft")
    with pytest.raises(RuntimeError, match="ODDISH_UNIKRAFT_ENABLED"):
        modal_app.assert_unikraft_default_enabled()


def test_unikraft_default_with_backend_and_pin_passes(monkeypatch):
    _configure(
        monkeypatch,
        ODDISH_HARBOR_ENVIRONMENT="unikraft",
        ODDISH_UNIKRAFT_ENABLED="true",
    )
    monkeypatch.setattr(
        modal_app.harbor_source, "UNIKRAFT_ENVIRONMENT", object()
    )
    modal_app.assert_unikraft_default_enabled()


def test_unikraft_default_without_the_harbor_pin_blocks_the_deploy(monkeypatch):
    # Registration needs the flag AND a pin that defines the environment;
    # the deploy venv resolves the same pin the worker image bakes.
    _configure(
        monkeypatch,
        ODDISH_HARBOR_ENVIRONMENT="unikraft",
        ODDISH_UNIKRAFT_ENABLED="true",
    )
    monkeypatch.setattr(modal_app.harbor_source, "UNIKRAFT_ENVIRONMENT", None)
    with pytest.raises(RuntimeError, match="Harbor pin"):
        modal_app.assert_unikraft_default_enabled()


def test_the_dotenv_default_counts_too(monkeypatch):
    # The deploy reads backend/.env as well as the process environment.
    _configure(monkeypatch)
    monkeypatch.setattr(
        modal_app, "LOCAL_DOTENV_VARS", {"ODDISH_HARBOR_ENVIRONMENT": "unikraft"}
    )
    with pytest.raises(RuntimeError, match="ODDISH_UNIKRAFT_ENABLED"):
        modal_app.assert_unikraft_default_enabled()
