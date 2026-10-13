from datetime import datetime, timezone

import pytest

from oddish.core import admin, model_concurrency
from oddish.core.admin import LoadQueue, LoadSnapshot, LoadTotals


def _queue(queue_key: str, *, queued: int, running: int) -> LoadQueue:
    return LoadQueue(
        queue_key=queue_key,
        queued=queued,
        running=running,
        limit=256,
        fill=None,
        wait_p95_seconds=None,
        advisory_limit=256,
        limit_source="static",
    )


def _snapshot(*queues: LoadQueue) -> LoadSnapshot:
    return LoadSnapshot(
        submit_ceiling=64,
        pressure=0.0,
        ttl_seconds=5,
        totals=LoadTotals(queued=0, running=0),
        queues=list(queues),
        timestamp="2026-10-13T00:00:00",
    )


@pytest.mark.asyncio
async def test_configured_limits_let_the_database_win_and_drop_non_models(
    monkeypatch,
):
    monkeypatch.setattr(
        model_concurrency.settings,
        "model_concurrency_overrides",
        {"openai/gpt-6-luna": 64, "zai/glm-5.3": 32, "Gemini/Gemini-3.7-Flash": 8},
    )

    async def stored(session):
        return {
            "zai/glm-5.3": 0,
            "anthropic/claude-sonnet-5-5": 80,
            "nop_oracle": 1500,
            "analysis": 250,
            "verdict": 256,
            "task_expand": 16,
        }

    monkeypatch.setattr(model_concurrency, "get_model_concurrency_overrides", stored)

    limits = await model_concurrency.get_configured_model_limits(None)

    # A database 0 disables a deploy-configured model; pipeline queues are
    # not models; keys are normalized like every other queue read.
    assert limits == {
        "openai/gpt-6-luna": 64,
        "gemini/gemini-3.7-flash": 8,
        "anthropic/claude-sonnet-5-5": 80,
    }


@pytest.mark.asyncio
async def test_configured_models_join_load_and_rank_by_health(monkeypatch):
    async def limits(session):
        return {
            "xai/grok-4.6": 256,
            "openai/gpt-6-luna": 350,
            "anthropic/claude-opus-5-5": 100,
            "global.anthropic.claude-sonnet-5-5": 200,
        }

    monkeypatch.setattr(admin, "get_configured_model_limits", limits)
    checked = datetime(2026, 10, 13, tzinfo=timezone.utc)
    snapshot = _snapshot(
        _queue("openai/gpt-6-luna", queued=3, running=27),
        _queue("meta/unconfigured-model", queued=9, running=1),
    )

    response = await admin.get_configured_models_core(
        None,
        snapshot=snapshot,
        health={
            "xai/grok-4.6": ("failing", checked),
            "openai/gpt-6-luna": ("healthy", checked),
            "anthropic/claude-opus-5-5": ("healthy", checked),
        },
    )

    assert response.timestamp == "2026-10-13T00:00:00"
    assert [(m.model, m.health) for m in response.models] == [
        ("anthropic/claude-opus-5-5", "healthy"),
        ("openai/gpt-6-luna", "healthy"),
        ("global.anthropic.claude-sonnet-5-5", "unchecked"),
        ("xai/grok-4.6", "failing"),
    ]
    luna = response.models[1]
    assert (luna.provider, luna.limit, luna.running, luna.queued) == (
        "openai",
        350,
        27,
        3,
    )
    assert luna.last_checked_at == checked
    bedrock = response.models[2]
    assert bedrock.provider == "bedrock"
    assert (bedrock.running, bedrock.queued, bedrock.last_checked_at) == (0, 0, None)


@pytest.mark.asyncio
async def test_configured_models_without_a_monitor_are_unchecked(monkeypatch):
    async def limits(session):
        return {"zai/glm-5.3": 300, "deepseek/deepseek-v4-flash": 300}

    monkeypatch.setattr(admin, "get_configured_model_limits", limits)

    response = await admin.get_configured_models_core(None, snapshot=_snapshot())

    assert [(m.model, m.health) for m in response.models] == [
        ("deepseek/deepseek-v4-flash", "unchecked"),
        ("zai/glm-5.3", "unchecked"),
    ]
