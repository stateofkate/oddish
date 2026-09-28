"""Sonnet 5.5 routing, QA execution, and launch-day pricing regressions."""

import pytest

from oddish.config import Settings
from oddish.model_pricing import ModelPricing, get_model_pricing


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5-5", "claude-sonnet-5.5", "bedrock/claude-sonnet-5-5"]
)
def test_sonnet_55_bedrock_route(model):
    settings = Settings(_env_file=None, claude_code_force_direct_api=False)
    expected = "global.anthropic.claude-sonnet-5-5"
    assert settings.normalize_trial_model("claude-code", model) == expected
    assert settings.get_queue_key_for_trial("claude-code", model) == expected
    assert settings.get_provider_for_trial("claude-code", model) == "bedrock"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5-5",
        "anthropic/claude-sonnet-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "vertex_ai/claude-sonnet-5-5",
    ],
)
def test_sonnet_55_pricing_without_catalog_update(monkeypatch, model):
    monkeypatch.setattr("oddish.model_pricing._find_litellm_pricing", lambda _: None)
    assert get_model_pricing(model) == ModelPricing(
        input=2e-6, output=10e-6, cache_read=0.2e-6, cache_write=2.5e-6
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider",
    [
        ("anthropic/claude-sonnet-5-5", "anthropic"),
        ("claude-sonnet-5-5", "bedrock"),
        ("vertex_ai/claude-sonnet-5-5", "vertex_ai"),
    ],
)
async def test_sonnet_55_qa_eval_preserves_model_effort_and_evidence(
    monkeypatch, model, provider
):
    from oddish.db import TaskModel
    from oddish.schemas import TrialSpec
    from oddish.workers import analysis_trials

    settings = Settings(_env_file=None, claude_code_force_direct_api=False)
    monkeypatch.setattr(analysis_trials, "settings", settings)
    queued = {}

    class Session:
        def add(self, row):
            self.row = row

        async def flush(self):
            pass

    async def reserve(*args, **kwargs):
        return 1

    async def enqueue(*args, **kwargs):
        queued.update(kwargs)

    monkeypatch.setattr("oddish.queue.reserve_next_trial_index", reserve)
    monkeypatch.setattr("oddish.queue.enqueue_trial_worker_job", enqueue)
    execution = TrialSpec(
        agent="claude-code",
        model=model,
        agent_config={"kwargs": {"reasoning_effort": "max"}},
    )
    trial = await analysis_trials.create_analysis_trial(
        Session(),
        task=TaskModel(id="task-1", name="task-1", org_id="org-1"),
        kind="qa_eval",
        brief="Review the saved evidence",
        payload={"trial_ids": ["source-1"]},
        execution=execution,
        experiment_id="eval-1",
    )
    assert trial.agent == "claude-code"
    assert trial.provider == provider
    assert trial.model == settings.normalize_trial_model("claude-code", model)
    assert trial.harbor_config["agent_config"]["kwargs"]["reasoning_effort"] == "max"
    assert trial.harbor_config["analysis_payload"]["trial_ids"] == ["source-1"]
    assert queued["queue_key"] == trial.queue_key
