"""Identity uses the same approval and bound-key checks as other hosted routes."""

from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import auth
import pytest
from auth import AuthContext, AuthMethod, get_auth_context
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from models import APIKeyScope, UserRole

pytestmark = pytest.mark.asyncio

# Load only this route: api.routers imports the complete worker/provider tree.
spec = spec_from_file_location(
    "identity_route", Path(__file__).parents[1] / "api/routers/identity.py"
)
module = module_from_spec(spec)
spec.loader.exec_module(module)
router = module.router


async def call(monkeypatch, context=None, *, approved=True):
    app = FastAPI()
    app.include_router(router)
    if context is not None:
        app.dependency_overrides[get_auth_context] = lambda: context

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=None))

    async def approval(org_id, *, session):
        assert org_id == "org-1"
        if not approved:
            raise HTTPException(403, "Organization is not approved")
        return SimpleNamespace(id=org_id, slug="current-org-slug")

    monkeypatch.setattr(auth, "get_read_session", session)
    monkeypatch.setattr(auth, "require_execution_org", approval)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/auth/identity")


async def test_identity_returns_scope_and_actor_without_credentials(monkeypatch):
    context = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        org_slug="old-slug",
        user_id="user-1",
        user_role=UserRole.ADMIN,
        api_key_id="key-1",
        api_key_created_by_role="member",
        scope=APIKeyScope.TASKS,
    )
    response = await call(monkeypatch, context)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "auth_method": "api_key",
        "org_id": "org-1",
        "org_slug": "current-org-slug",
        "user_id": "user-1",
        "user_role": "admin",
        "api_key_id": "key-1",
        "api_key_created_by_role": "member",
        "scope": "tasks",
    }


async def test_clerk_identity_and_read_key_keep_their_scope(monkeypatch):
    for method, scope in [
        (AuthMethod.CLERK_JWT, APIKeyScope.FULL),
        (AuthMethod.API_KEY, APIKeyScope.READ),
    ]:
        context = AuthContext(
            method=method, org_id="org-1", user_id="user-1", scope=scope
        )
        response = await call(monkeypatch, context)
        assert response.status_code == 200
        assert response.json()["scope"] == scope.value


async def test_anonymous_and_unapproved_org_are_denied(monkeypatch):
    assert (await call(monkeypatch)).status_code == 401
    context = AuthContext(method=AuthMethod.API_KEY, org_id="org-1", api_key_id="key-1")
    assert (await call(monkeypatch, context, approved=False)).status_code == 403


async def test_internal_analysis_key_cannot_become_service_identity(monkeypatch):
    context = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        api_key_id="key-1",
        scope=APIKeyScope.READ,
        bound_analysis_trial_id="analysis-1",
    )
    assert (await call(monkeypatch, context)).status_code == 403
