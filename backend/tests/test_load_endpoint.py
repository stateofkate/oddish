from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from api.routers import load as load_router
from auth import AuthContext, AuthMethod, require_auth
from models import APIKeyScope
from oddish.core.admin import LoadQueue, LoadSnapshot, LoadTotals
from oddish.db import utcnow


def _snapshot() -> LoadSnapshot:
    return LoadSnapshot(
        submit_ceiling=64,
        pressure=0.0,
        ttl_seconds=5,
        totals=LoadTotals(queued=0, running=0),
        queues=[],
        timestamp="2026-06-22T00:00:00",
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(load_router.router)
    return app


@pytest.mark.asyncio
async def test_load_requires_auth_returns_401_anonymous(monkeypatch):
    async def fake_snap():
        return _snapshot()

    monkeypatch.setattr(load_router, "get_cached_load_snapshot", fake_snap)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/load")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_load_authed_returns_snapshot(monkeypatch):
    async def fake_snap():
        return _snapshot()

    monkeypatch.setattr(load_router, "get_cached_load_snapshot", fake_snap)
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id="org_1"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/load")
    assert resp.status_code == 200
    body = resp.json()
    assert body["submit_ceiling"] == 64
    assert body["pressure"] == 0.0
    assert body["ttl_seconds"] == 5
    assert body["totals"] == {"queued": 0, "running": 0}
    assert body["queues"] == []


def _queue(queue_key: str, *, queued: int = 0, running: int = 0) -> LoadQueue:
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


def _client_as(monkeypatch, org_id: str, *, queues=()) -> httpx.AsyncClient:
    async def fake_snap():
        return _snapshot().model_copy(update={"queues": list(queues)})

    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_operator")
    monkeypatch.setattr(load_router, "get_cached_load_snapshot", fake_snap)
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id=org_id, scope=APIKeyScope.READ
    )
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_load_hides_queue_keys_outside_the_operator_org(monkeypatch):
    queues = [_queue("xai/vendor-private-model", queued=3, running=1)]

    async with _client_as(monkeypatch, "org_customer", queues=queues) as client:
        customer = (await client.get("/load")).json()
    async with _client_as(monkeypatch, "org_operator", queues=queues) as client:
        operator = (await client.get("/load")).json()

    assert customer["queues"] == []
    assert customer["submit_ceiling"] == 64
    assert customer["totals"] == {"queued": 0, "running": 0}
    assert [q["queue_key"] for q in operator["queues"]] == ["xai/vendor-private-model"]


@pytest.mark.asyncio
async def test_models_need_the_operator_org(monkeypatch):
    async with _client_as(monkeypatch, "org_customer") as client:
        response = await client.get("/models")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_models_join_limits_load_and_monitor_health(monkeypatch):
    import endpoint_health
    import oddish.core.admin as core_admin

    now = utcnow()
    monitors = [
        ("m-sonnet", "anthropic/claude-sonnet-5-5", "success", now),
        # An explicit ODDISH_ENDPOINT_MONITORS entry: its id is not the derived
        # one, but its name is the queue key.
        ("explicit-glm", "zai/glm-5.3", "failure", now),
        ("m-luna", "openai/gpt-6-luna", "success", now - timedelta(hours=2)),
        # One connection probes both Gemini aliases and keeps one alias's name.
        ("m-gemini", "google/gemini-3.7-flash", "success", now),
    ]

    class _Session:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: monitors)

    @asynccontextmanager
    async def read_session():
        yield _Session()

    async def limits(_session):
        return {
            "anthropic/claude-sonnet-5-5": 80,
            "zai/glm-5.3": 300,
            "openai/gpt-6-luna": 350,
            "gemini/gemini-3.7-flash": 100,
            "google/gemini-3.7-flash": 20,
        }

    async def target_index(_session):
        return [], {
            "anthropic/claude-sonnet-5-5": "m-sonnet",
            "zai/glm-5.3": "derived-glm",
            "openai/gpt-6-luna": "m-luna",
            "gemini/gemini-3.7-flash": "m-gemini",
            "google/gemini-3.7-flash": "m-gemini",
        }

    monkeypatch.setattr(load_router, "get_read_session", read_session)
    monkeypatch.setattr(core_admin, "get_configured_model_limits", limits)
    monkeypatch.setattr(endpoint_health, "platform_target_index", target_index)
    queues = [_queue("anthropic/claude-sonnet-5-5", queued=4, running=12)]

    async with _client_as(monkeypatch, "org_operator", queues=queues) as client:
        response = await client.get("/models")

    assert response.status_code == 200, response.text
    # Both Gemini aliases share their connection's health, the explicit monitor
    # matches by name, a stale success is unchecked, and failing sorts last.
    assert [
        (m["model"], m["health"], m["limit"], m["running"], m["queued"])
        for m in response.json()["models"]
    ] == [
        ("anthropic/claude-sonnet-5-5", "healthy", 80, 12, 4),
        ("gemini/gemini-3.7-flash", "healthy", 100, 0, 0),
        ("google/gemini-3.7-flash", "healthy", 20, 0, 0),
        ("openai/gpt-6-luna", "unchecked", 350, 0, 0),
        ("zai/glm-5.3", "failing", 300, 0, 0),
    ]
