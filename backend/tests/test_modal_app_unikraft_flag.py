"""The Unikraft opt-in must be baked into the image env and gate its secret,
mirroring Numinous: the worker's registry re-reads settings from the image env,
where neither the deploy shell nor ``backend/.env`` exists."""

from __future__ import annotations

import importlib
import sys


def _fresh_modal_app():
    """Import ``modal_app`` afresh so its deploy-time reads re-run.

    Not ``importlib.reload``: other modules in this suite drop ``modal_app``
    from ``sys.modules`` to force their own fresh import and do not put it
    back, and reload raises ImportError on a module that is not there.
    """
    sys.modules.pop("modal_app", None)
    return importlib.import_module("modal_app")


def test_flag_and_prebuilt_requirement_are_baked():
    app = _fresh_modal_app()
    assert app._UNIKRAFT_ENABLED_ENV == "ODDISH_UNIKRAFT_ENABLED"
    assert app._UNIKRAFT_ENABLED_ENV in app.ENV_VARS
    # Hosted workers have no BuildKit; Harbor must refuse to build.
    assert app.ENV_VARS["ODDISH_UNIKRAFT_REQUIRE_PREBUILT_IMAGE"] == "true"


def test_secret_name_override_is_resolved_and_baked(monkeypatch):
    # The name resolves like the Thunder name (env, then backend/.env, then
    # the default) and the resolved value must ride the image env.
    monkeypatch.setenv("ODDISH_UNIKRAFT_SECRET_NAME", "custom-unikraft")
    app = _fresh_modal_app()
    try:
        assert app._UNIKRAFT_SECRET_NAME == "custom-unikraft"
        assert app.ENV_VARS[app._UNIKRAFT_SECRET_NAME_ENV] == "custom-unikraft"
    finally:
        monkeypatch.undo()
        _fresh_modal_app()


def test_disabled_deploy_has_no_secret(monkeypatch):
    monkeypatch.delenv("ODDISH_UNIKRAFT_ENABLED", raising=False)
    app = _fresh_modal_app()
    try:
        assert app.UNIKRAFT_SECRET_PLAN == []
        assert app.ENV_VARS[app._UNIKRAFT_ENABLED_ENV] == "false"
    finally:
        monkeypatch.undo()
        _fresh_modal_app()


def test_enabled_deploy_attaches_secret(monkeypatch):
    monkeypatch.setenv("ODDISH_UNIKRAFT_ENABLED", "1")
    app = _fresh_modal_app()
    try:
        assert app.UNIKRAFT_SECRET_PLAN == ["oddish-unikraft"]
        assert app.ENV_VARS[app._UNIKRAFT_ENABLED_ENV] == "true"
    finally:
        monkeypatch.undo()
        _fresh_modal_app()
