from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole
from oddish.schemas import QAEvalCreateResponse, QAEvalTrialResponse

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
async def test_post_qa_evals_needs_an_org_admin(monkeypatch, auth):
    async with _client(monkeypatch, auth) as client:
        response = await client.post("/qa-evals", json={})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_replaying_an_unknown_prompt_hash_is_not_found(monkeypatch):
    async with _client(monkeypatch) as client:
        response = await client.post(
            "/qa-evals",
            json={"name": "replay", "goldens": True, "prompt_sha256": "0" * 64},
        )
    assert response.status_code == 404, response.text
    assert "no recorded post_trial prompt" in response.json()["detail"]
