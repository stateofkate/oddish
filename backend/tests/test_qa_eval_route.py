from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole
from oddish.db import utcnow
from oddish.analyze.qa_stages import QAStage, shipped_prompt, shipped_prompt_sha256
from oddish.schemas import (
    QAEvalCase,
    QAEvalCreateResponse,
    QAEvalErrorGroup,
    QAEvalRunResponse,
    QAEvalRunSummary,
    QAEvalSpend,
    QAEvalStatusResponse,
    QAEvalTrialResponse,
    QAGoldensSummary,
)

_ROUTER_PATH = Path(__file__).resolve().parents[1] / "api" / "routers" / "qa_eval.py"
_SPEC = spec_from_file_location("qa_eval_route_under_test", _ROUTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
qa_eval = module_from_spec(_SPEC)
_SPEC.loader.exec_module(qa_eval)

ADMIN = AuthContext(
    method=AuthMethod.CLERK_JWT,
    org_id="org-1",
    user_id="user-1",
    user_role=UserRole.ADMIN,
)


class _EmptySession:
    """A database with no prompt rows and no replays."""

    committed = False

    async def scalars(self, _statement):
        return []

    async def execute(self, _statement):
        return SimpleNamespace(all=lambda: [])

    async def commit(self):
        self.committed = True


def _client(monkeypatch, auth: AuthContext = ADMIN) -> AsyncClient:
    session = _EmptySession()

    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr(qa_eval, "get_session", fake_session)
    monkeypatch.setattr(qa_eval, "get_read_session", fake_session)
    monkeypatch.setattr(qa_eval, "invalidate_dashboard_cache", lambda **_: None)
    app = FastAPI()
    app.include_router(qa_eval.router)
    app.dependency_overrides[require_auth] = lambda: auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_post_qa_evals_passes_org_and_owner_to_shared_core(monkeypatch):
    captured: dict = {}

    async def fake_create_core(_session, **kwargs):
        captured.update(kwargs)
        return QAEvalCreateResponse(
            experiment_id="experiment-1",
            experiment_name=kwargs["request"].name,
            prompt_name=kwargs["request"].prompt_name,
            prompt_sha256="abc",
            model="canonical-model",
            trials=[
                QAEvalTrialResponse(
                    source_trial_id="source-1", qa_eval_trial_id="eval-1"
                )
            ],
        )

    monkeypatch.setattr(qa_eval, "create_qa_eval_core", fake_create_core)
    async with _client(monkeypatch) as client:
        response = await client.post(
            "/qa-evals",
            headers={"Idempotency-Key": "stable-key"},
            json={
                "name": "candidate experiment",
                "source_trial_ids": ["source-1"],
                "prompt_name": "candidate-1",
                "prompt_text": "Classify the source.",
                "audit_context": "none",
                "environment": "modal",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == []
    assert captured["org_id"] == "org-1"
    assert captured["owner_user_id"] == "user-1"
    assert captured["billed_user_id"] == "user-1"
    assert captured["request"].source_trial_ids == ["source-1"]
    assert captured["request"].audit_context == "none"
    assert captured["request"].environment == "modal"
    assert captured["idempotency_key"] == "stable-key"
    assert captured["request_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth",
    [
        AuthContext(
            method=AuthMethod.CLERK_JWT, org_id="org-1", user_role=UserRole.MEMBER
        ),
        AuthContext(method=AuthMethod.API_KEY, org_id="org-1", scope=APIKeyScope.TASKS),
    ],
)
async def test_every_qa_eval_route_needs_an_org_admin(monkeypatch, auth):
    async with _client(monkeypatch, auth) as client:
        responses = [
            await client.post("/qa-evals", json={}),
            await client.get("/qa-evals/status"),
            await client.get(f"/qa-evals/versions/{'0' * 64}"),
            await client.get("/qa-evals/runs/experiment-1"),
            await client.get("/qa-evals/models"),
        ]
    assert [r.status_code for r in responses] == [403] * 5


@pytest.mark.asyncio
async def test_replaying_an_unknown_prompt_hash_is_not_found(monkeypatch):
    async with _client(monkeypatch) as client:
        response = await client.post(
            "/qa-evals",
            json={"name": "replay", "goldens": True, "prompt_sha256": "0" * 64},
        )
    assert response.status_code == 404, response.text
    assert "no recorded post_trial prompt" in response.json()["detail"]


@pytest.mark.asyncio
async def test_status_parses_the_stage(monkeypatch):
    captured: list[dict] = []

    async def fake_status(_session, **kwargs):
        captured.append(kwargs)
        return QAEvalStatusResponse(
            replayable=True,
            production_prompt_sha256="abc",
            goldens=QAGoldensSummary(counted=0, waiting=0, stale=0, labels=[]),
            cost_per_trial=None,
            spend=QAEvalSpend(total_usd=0, trials=0, cost_missing=0, by_model=[]),
            runs=[],
            production_run_id=None,
            frontier=[],
            models=[],
            versions=[],
        )

    monkeypatch.setattr(qa_eval, "qa_eval_status_core", fake_status)
    async with _client(monkeypatch) as client:
        default = await client.get("/qa-evals/status")
        chosen = await client.get("/qa-evals/status", params={"stage": "pre_trial"})
        unknown = await client.get("/qa-evals/status", params={"stage": "mid_trial"})

    assert (default.status_code, chosen.status_code) == (200, 200)
    assert unknown.status_code == 422
    assert captured == [
        {"org_id": "org-1", "stage": QAStage.POST_TRIAL},
        {"org_id": "org-1", "stage": QAStage.PRE_TRIAL},
    ]


@pytest.mark.asyncio
async def test_version_text_is_the_shipped_prompt_or_not_found(monkeypatch):
    sha = shipped_prompt_sha256(QAStage.PRE_TRIAL)
    async with _client(monkeypatch) as client:
        shipped = await client.get(
            f"/qa-evals/versions/{sha}", params={"stage": "pre_trial"}
        )
        # The pre-trial prompt's hash is no post-trial version.
        other_stage = await client.get(f"/qa-evals/versions/{sha}")

    assert shipped.status_code == 200, shipped.text
    assert shipped.json() == {"sha256": sha, "text": shipped_prompt(QAStage.PRE_TRIAL)}
    assert other_stage.status_code == 404


@pytest.mark.asyncio
async def test_an_unknown_run_is_not_found(monkeypatch):
    async with _client(monkeypatch) as client:
        response = await client.get("/qa-evals/runs/experiment-1")
    assert response.status_code == 404
    assert "experiment-1" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_run_is_returned_with_its_cases(monkeypatch):
    captured: list[dict] = []
    run = QAEvalRunResponse(
        summary=QAEvalRunSummary(
            experiment_id="experiment-1",
            experiment_name="qa-eval",
            title="prompt",
            created_at="2026-09-24T10:00:00Z",
            model="claude-sonnet-5",
            model_name="claude-sonnet-5",
            prompt_sha256="abc",
            subset=False,
            charted=True,
            replays=1,
            with_golden=1,
            judged=1,
            correct=0,
            errored=0,
            pending=0,
            accuracy=0.0,
            cost_usd=0.5,
            cost_per_trial=0.5,
            cost_missing=0,
        ),
        error_groups=[QAEvalErrorGroup(message="Timeout", count=1)],
        cases=[
            QAEvalCase(
                qa_eval_trial_id="qa-1",
                task_id="task-1",
                source_trial_id="trial-1",
                original_trial_id="trial-1",
                group="wrong",
                outcome="wrong",
                golden_id="golden-1",
                expected="BAD_FAILURE",
                got="GOOD_FAILURE",
                cost_usd=0.5,
                error=None,
            )
        ],
    )

    async def fake_run(_session, **kwargs):
        captured.append(kwargs)
        return run

    monkeypatch.setattr(qa_eval, "get_qa_eval_run_core", fake_run)
    async with _client(monkeypatch) as client:
        response = await client.get("/qa-evals/runs/experiment-1")

    assert response.status_code == 200, response.text
    assert QAEvalRunResponse.model_validate(response.json()) == run
    assert captured == [{"org_id": "org-1", "experiment_id": "experiment-1"}]


@pytest.mark.asyncio
async def test_models_rank_monitored_models_by_health(monkeypatch):
    now = utcnow()
    rows = [
        ("m1", "xai/grok-4.6", "failure", now),
        ("m2", "openai/gpt-6-luna", "success", now - timedelta(hours=2)),
        ("m3", "deepseek/deepseek-v4-flash", "monitor_error", now),
        ("m4", "anthropic/claude-sonnet-5-5", "success", now),
        ("m5", "google/gemini-3.7-flash", None, None),
    ]

    class _Monitors(_EmptySession):
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def monitors():
        yield _Monitors()

    async with _client(monkeypatch) as client:
        monkeypatch.setattr(qa_eval, "get_read_session", monitors)
        monkeypatch.setattr(qa_eval, "require_operator_org", lambda _auth: None)
        monkeypatch.setattr(qa_eval.settings, "qa_model", "anthropic/claude-sonnet-5")
        response = await client.get("/qa-evals/models")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["production_model"] == "anthropic/claude-sonnet-5"
    # A stale success and a broken check are unchecked, not healthy.
    assert [(m["model"], m["health"]) for m in body["models"]] == [
        ("anthropic/claude-sonnet-5-5", "healthy"),
        ("deepseek/deepseek-v4-flash", "unchecked"),
        ("google/gemini-3.7-flash", "unchecked"),
        ("openai/gpt-6-luna", "unchecked"),
        ("xai/grok-4.6", "failing"),
    ]


@pytest.mark.asyncio
async def test_models_need_the_operator_org(monkeypatch):
    async with _client(monkeypatch) as client:
        response = await client.get("/qa-evals/models")
    assert response.status_code == 403
