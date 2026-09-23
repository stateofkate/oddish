"""The launcher scopes the sandbox's model env by the trial's stored Harbor config.

A row that carries no model still names one in its ``agent_config``, and the
runner in the sandbox follows that model. The worker path hands the stored
config to ``scoped_model_env`` (via ``build_bundle``); keyed on the bare row
model alone, the launcher would ship the agent's default bundle instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.workers.queue import monitor_launch as ml


def _job():
    return SimpleNamespace(
        id="job-1",
        attempts=1,
        subject_id="trial-1",
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
        reroute_from_environment=None,
    )


@pytest.mark.asyncio
async def test_launch_forwards_the_stored_harbor_config(monkeypatch):
    model = "vertex_ai/claude-sonnet-5"
    stored = {"agent_config": {"name": "claude-code", "model_name": model}}
    prepared = SimpleNamespace(
        trial_environment="modal",
        task_id="task-1",
        task_version=1,
        trial_agent="claude-code",
        trial_model=None,
        trial_harbor_config=stored,
    )
    starter = SimpleNamespace(provider="modal")
    seen: list = []

    def _scoped(**kwargs):
        seen.append(kwargs.get("raw_harbor_config"))
        return {}

    monkeypatch.setattr(ml, "control_plane_url", lambda: "https://api.test")
    monkeypatch.setattr(ml, "record_monitor_heartbeat", _noop)
    monkeypatch.setattr(ml, "_prepare_trial_run", lambda **k: _val(prepared))
    monkeypatch.setattr(
        ml.monitor_sandbox, "provider_for_environment", lambda e: "modal"
    )
    monkeypatch.setattr(ml.monitor_sandbox, "get_starter", lambda p: starter)
    monkeypatch.setattr(
        ml.monitor_sandbox.SandboxSize,
        "from_span_resources",
        staticmethod(lambda r, **kw: None),
    )
    monkeypatch.setattr(ml, "_resolve_image", _noop)
    monkeypatch.setattr(ml, "_claim_funding_key", lambda **k: _val((True, {})))
    monkeypatch.setattr(ml, "_task_facts", lambda *a, **k: _val((None, None, {})))
    monkeypatch.setattr(
        ml.monitor_token, "mint_monitor_credential", lambda *a: _val("tok")
    )
    monkeypatch.setattr(ml.job_tokens, "scoped_model_env", _scoped)
    monkeypatch.setattr(
        ml.monitor_image_cache, "resolve", lambda *a, **k: _val(("img", None))
    )
    monkeypatch.setattr(ml, "warm_source_for_trial", _noop)
    monkeypatch.setattr(ml, "_start", lambda *a, **k: _val("ext-1"))
    monkeypatch.setattr(ml, "_persist_handle", lambda *a, **k: _val(True))
    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _noop)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is not None and outcome.handoff is not None
    assert seen == [stored]


async def _noop(*a, **k):
    return None


async def _val(v):
    return v
