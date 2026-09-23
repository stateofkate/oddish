"""The oddish-mon rollout levers set by a deploy workflow reach the worker
image env; unset ones leave the code defaults alone."""

import importlib

import modal_app


def test_monitor_execution_env_lines_are_baked_into_the_worker_image(monkeypatch):
    monkeypatch.setattr(
        modal_app.modal.Secret, "from_dict", lambda values, *a, **k: values
    )
    monkeypatch.setenv("ODDISH_MONITOR_EXECUTION_SAMPLE_PERCENT", "50")
    monkeypatch.setenv("ODDISH_MONITOR_EXECUTION_ENABLED", "false")
    monkeypatch.delenv(
        "ODDISH_MONITOR_EXECUTION_FALLBACK_AFTER_ATTEMPTS", raising=False
    )
    try:
        importlib.reload(modal_app)
        assert modal_app.ENV_VARS["ODDISH_MONITOR_EXECUTION_SAMPLE_PERCENT"] == "50"
        assert modal_app.ENV_VARS["ODDISH_MONITOR_EXECUTION_ENABLED"] == "false"
        assert (
            "ODDISH_MONITOR_EXECUTION_FALLBACK_AFTER_ATTEMPTS" not in modal_app.ENV_VARS
        )
    finally:
        monkeypatch.delenv("ODDISH_MONITOR_EXECUTION_SAMPLE_PERCENT")
        monkeypatch.delenv("ODDISH_MONITOR_EXECUTION_ENABLED")
        importlib.reload(modal_app)
