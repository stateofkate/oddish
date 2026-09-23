"""Curated model ids against a real server: old clients unchanged, new CLI checked.

A released CLI never sends ``allow_unknown_model``; the raw POSTs below are
the body it builds for ``oddish run``. The current CLI runs as a subprocess.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from .conftest import DB_URL, E2E_ENABLED, cli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]


async def _trial_models(task_id: str) -> list[str]:
    from oddish.db import TrialModel, get_session

    async with get_session() as session:
        rows = await session.scalars(
            select(TrialModel.model).where(TrialModel.task_id == task_id)
        )
        return sorted(rows)


def _released_client_post(base: str, api_key: str, task_id: str, model: str):
    return httpx.post(
        f"{base}/tasks/sweep",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "task_id": task_id,
            "configs": [{"agent": "mini-swe-agent", "model": model, "n_trials": 1}],
            "priority": "low",
            "run_probe": False,
            "gate_baselines": False,
        },
        timeout=30.0,
    )


async def test_released_client_body_still_submits_unlisted_ids(live_server, seeded):
    for model in ("fireworks/some-unlisted-model", "deepseek-v5-preview"):
        resp = _released_client_post(
            live_server, seeded["api_key"], seeded["task_id"], model
        )
        assert resp.status_code == 200, resp.text

    assert await _trial_models(seeded["task_id"]) == [
        "deepseek/deepseek-v5-preview",
        "fireworks/some-unlisted-model",
    ]


def _cli_run(live_server, seeded, model: str, *extra: str):
    return cli(
        live_server,
        seeded["api_key"],
        "run",
        "--task",
        seeded["task_id"],
        "--agent",
        "mini-swe-agent",
        "--model",
        model,
        "--n-trials",
        "1",
        "--background",
        "--json",
        *extra,
    )


async def test_cli_rejects_unknown_id_with_the_right_one(live_server, seeded):
    proc = _cli_run(live_server, seeded, "fireworks/deepseek-v4-flash")

    assert proc.returncode != 0
    assert "fireworks/deepseek-v4-flash-0731" in proc.stdout + proc.stderr
    assert await _trial_models(seeded["task_id"]) == []


async def test_cli_allow_unknown_model_submits(live_server, seeded):
    proc = _cli_run(
        live_server, seeded, "fireworks/some-unlisted-model", "--allow-unknown-model"
    )

    assert proc.returncode == 0, proc.stderr
    assert await _trial_models(seeded["task_id"]) == ["fireworks/some-unlisted-model"]


async def test_cli_bare_deepseek_routes_like_staging(live_server, seeded, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-e2e-fireworks")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    proc = _cli_run(live_server, seeded, "deepseek-v4-flash")

    assert proc.returncode == 0, proc.stderr
    assert await _trial_models(seeded["task_id"]) == ["deepseek/deepseek-v4-flash"]
