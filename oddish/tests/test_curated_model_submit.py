"""Curated Fireworks/DeepSeek ids: opt-in submit check, unchanged routing.

Released CLIs and the dashboard never send ``allow_unknown_model``. Their
payloads must validate and route exactly as before this check existed; only a
client that sends ``allow_unknown_model: false`` gets the 422.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException

from oddish.cli.api import load_sweep_config
from oddish.config import curated_model_error, settings
from oddish.core.idempotency import compute_request_hash
from oddish.core.sweeps import validate_sweep_submission
from oddish.schemas import AgentModelPair, TaskSweepSubmission

run_mod = importlib.import_module("oddish.cli.run")

# (agent, model as typed) -> stored model / queue key, captured from
# origin/staging before this change. Every row must stay identical.
_STAGING_ROUTES = [
    ("mini-swe-agent", "deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    ("mini-swe-agent", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    ("dsh", "deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro"),
    ("dsh", "deepseek-v5-preview", "deepseek/deepseek-v5-preview"),
    ("claude-code", "fireworks/glm-5.2", "fireworks/glm-5p2"),
    ("claude-code", "fw/kimi-k2.7", "fireworks/kimi-k2p7-code"),
    ("claude-code", "glm-5.2", "zai/glm-5.2"),
    ("claude-code", "minimax-m3", "minimax/minimax-m3"),
    (
        "claude-code",
        "fireworks/some-unlisted-model",
        "fireworks/some-unlisted-model",
    ),
    (
        "claude-code",
        "fireworks/accounts/fireworks/models/foo",
        "fireworks/accounts/fireworks/models/foo",
    ),
    (
        "mini-swe-agent",
        "fireworks/deepseek-v4-flash",
        "fireworks/deepseek-v4-flash",
    ),
    (
        "mini-swe-agent",
        "fireworks/deepseek-v4-flash-0731",
        "fireworks/deepseek-v4-flash-0731",
    ),
    ("codex", "openai/gpt-5.3-codex", "openai/gpt-5.3-codex"),
    ("codex", "azure/gpt-5.3-codex", "azure/gpt-5.3-codex"),
]


def _submission(model, agent="mini-swe-agent", **config_kwargs):
    return TaskSweepSubmission(
        task_id="task-curated",
        configs=[AgentModelPair(agent=agent, model=model, n_trials=1, **config_kwargs)],
    )


@pytest.mark.parametrize(("agent", "model", "stored"), _STAGING_ROUTES)
def test_released_client_payload_routes_as_on_staging(agent, model, stored):
    submission = _submission(model, agent=agent)
    before = submission.model_dump()

    validate_sweep_submission(submission)

    assert submission.model_dump() == before
    assert settings.normalize_trial_model(agent, model) == stored
    assert settings.get_queue_key_for_trial(agent, model) == stored


@pytest.mark.parametrize(
    "model",
    [
        "fireworks/some-unlisted-model",
        "fireworks/deepseek-v4-flash",
        "deepseek-v5-preview",
        "deepseek/deepseek-v5-preview",
    ],
)
def test_opted_in_client_gets_422_for_unknown_curated_id(model):
    with pytest.raises(HTTPException) as exc:
        validate_sweep_submission(_submission(model, allow_unknown_model=False))
    assert exc.value.status_code == 422
    assert "--allow-unknown-model" in exc.value.detail


def test_rejection_names_the_serverless_fireworks_id():
    error = curated_model_error("fireworks/deepseek-v4-flash")
    assert error and "fireworks/deepseek-v4-flash-0731" in error


@pytest.mark.parametrize(
    "model",
    [
        "fireworks/glm-5.2",
        "fw/kimi-k2p7-code",
        "fireworks/deepseek-v4-flash-0731",
        "fireworks/accounts/fireworks/models/anything",
        "deepseek-v4-flash",
        "deepseek/deepseek-v4-pro-0813",
        "deepseek-flash",
        "deepseek/deepseek-v4.1-flash",
        "fireworks/deepseek-v4.1-flash",
        "glm-5.2",
        "openai/gpt-5.3-codex",
        "anthropic/claude-sonnet-4-5",
    ],
)
def test_opted_in_client_accepts_known_and_open_provider_ids(model):
    validate_sweep_submission(_submission(model, allow_unknown_model=False))


def test_allow_unknown_model_true_sends_unlisted_id():
    validate_sweep_submission(
        _submission("fireworks/some-unlisted-model", allow_unknown_model=True)
    )


def test_nop_oracle_skip_the_curated_check():
    validate_sweep_submission(
        _submission("fireworks/whatever", agent="nop", allow_unknown_model=False)
    )


def test_curated_check_ignores_provider_credentials(monkeypatch):
    for name in ("FIREWORKS_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    without = curated_model_error("fireworks/some-unlisted-model")
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-fw")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    assert curated_model_error("fireworks/some-unlisted-model") == without
    assert curated_model_error("fireworks/glm-5.2") is None


def test_omitted_flag_keeps_the_released_client_request_hash():
    from oddish.core.idempotency import _canonical_digest

    captured: list[dict] = []

    def _capture(value):
        captured.append(value)
        return _canonical_digest(value)

    import oddish.core.idempotency as idem

    original = idem._canonical_digest
    idem._canonical_digest = _capture
    try:
        compute_request_hash(_submission("deepseek-v4-flash"))
    finally:
        idem._canonical_digest = original

    assert "allow_unknown_model" not in captured[0]["configs"][0]
    assert compute_request_hash(
        _submission("deepseek-v4-flash", allow_unknown_model=False)
    ) != compute_request_hash(_submission("deepseek-v4-flash"))


def _capture_run(monkeypatch):
    payloads: list[dict] = []

    def _post(_api_url, payload):
        payloads.append(payload)
        return {
            "id": "task-1",
            "trials_count": 1,
            "experiment_id": "exp-1",
            "experiment_name": "abt-1153",
            "providers": {},
        }

    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(run_mod, "post_sweep_payload", _post)
    monkeypatch.setattr(run_mod, "get_task_summary", lambda *a, **k: None)
    return payloads


@pytest.mark.parametrize("allow", [False, True])
def test_cli_sends_model_as_typed_with_explicit_flag(monkeypatch, allow):
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-fw")
    payloads = _capture_run(monkeypatch)

    run_mod.run(
        existing_task_id="task-1",
        agent="mini-swe-agent",
        model="deepseek-v4-flash",
        n_trials=1,
        allow_unknown_model=allow,
        json_output=False,
    )

    config = payloads[0]["configs"][0]
    assert config["model"] == "deepseek-v4-flash"
    assert config["allow_unknown_model"] is allow


def test_sweep_yaml_allow_unknown_model_is_per_agent(tmp_path):
    path = tmp_path / "sweep.yaml"
    path.write_text(
        "agents:\n"
        "  - name: claude-code\n"
        "    model_name: fireworks/private-model\n"
        "    allow_unknown_model: true\n"
        "  - name: claude-code\n"
        "    model_name: fireworks/glm-5.2\n"
    )
    agents = load_sweep_config(path)["agents"]
    assert agents[0]["allow_unknown_model"] is True
    assert "allow_unknown_model" not in agents[1]
