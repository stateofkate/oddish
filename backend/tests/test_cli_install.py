"""Install the CLI with an Oddish API key: a view-only org mints a read key,
then gets the newest client archive through ``/cli/latest``."""

import os
import uuid

import auth
import httpx
import pytest
import pytest_asyncio
from api.routers import api_keys, cli
from auth.types import AuthContext, AuthMethod
from fastapi import FastAPI, Request
from models import APIKeyModel, OrganizationModel, UserModel, UserRole
from oddish.db import get_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
)

ARCHIVE = "https://api.github.com/repos/abundant-ai/homebrew-tap/releases/assets/1"
SUMS = "https://api.github.com/repos/abundant-ai/homebrew-tap/releases/assets/2"
SIGNED = "https://release-assets.githubusercontent.com/oddish.tar.gz?sig=abc"


def github(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer gh-test"
    url = str(request.url)
    if url.endswith("/releases/latest"):
        return httpx.Response(
            200,
            json={
                "tag_name": "v0.1.23",
                "assets": [
                    {"name": "oddish-0.1.23.tar.gz", "url": ARCHIVE},
                    {"name": "SHA256SUMS", "url": SUMS},
                ],
            },
        )
    if url == SUMS:
        return httpx.Response(200, text="abc123  oddish-0.1.23.tar.gz\n")
    if url == ARCHIVE:
        return httpx.Response(302, headers={"location": SIGNED})
    return httpx.Response(404)


@pytest_asyncio.fixture
async def client(monkeypatch):
    org_id = f"cli_view_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id,
                name="Viewer",
                slug=org_id,
                viewer_access=True,
                execution_enabled=False,
                org_type="customer",
            )
        )
        await session.flush()
        session.add(
            UserModel(
                id=f"{org_id}_user",
                org_id=org_id,
                role=UserRole.ADMIN,
                email=f"{org_id}@example.com",
            )
        )

    async def fake_auth(request: Request):
        if not request.headers.get("authorization"):
            return AuthContext(method=AuthMethod.ANONYMOUS)
        return AuthContext(
            method=AuthMethod.CLERK_JWT,
            org_id=org_id,
            user_id=f"{org_id}_user",
            user_role=UserRole.ADMIN,
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        cli.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(github), **kw),
    )
    monkeypatch.setenv("ODDISH_CLI_RELEASE_TOKEN", "gh-test")
    cli._cache.clear()
    app = FastAPI()
    app.include_router(cli.router)
    app.include_router(api_keys.router)
    app.dependency_overrides[auth.get_auth_context] = fake_auth
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    async with get_session() as session:
        await session.execute(
            APIKeyModel.__table__.delete().where(APIKeyModel.org_id == org_id)
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(OrganizationModel.id == org_id)
        )


SIGNED_IN = {"Authorization": "Bearer user"}


@pytest.mark.asyncio
async def test_view_only_org_mints_a_read_key_but_nothing_wider(client):
    read = await client.post(
        "/api-keys", json={"name": "cli", "scope": "read"}, headers=SIGNED_IN
    )
    assert read.status_code == 200, read.text
    assert read.json()["scope"] == "read"
    full = await client.post(
        "/api-keys", json={"name": "cli", "scope": "full"}, headers=SIGNED_IN
    )
    assert full.status_code == 403
    # The org can also revoke the key it made.
    revoked = await client.delete(f"/api-keys/{read.json()['id']}", headers=SIGNED_IN)
    assert revoked.status_code == 200, revoked.text


@pytest.mark.asyncio
async def test_latest_cli_hands_out_a_signed_link(client):
    assert (await client.get("/cli/latest")).status_code == 401
    latest = await client.get("/cli/latest", headers=SIGNED_IN)
    assert latest.status_code == 200, latest.text
    assert latest.json() == {"version": "0.1.23", "sha256": "abc123", "url": SIGNED}
    text = await client.get("/cli/latest?format=text", headers=SIGNED_IN)
    assert text.text == f"0.1.23 abc123 {SIGNED}\n"


@pytest.mark.asyncio
async def test_install_script_is_public(client):
    script = await client.get("/cli/install.sh")
    assert script.status_code == 200
    assert "ODDISH_API_KEY" in script.text and "uv sync" in script.text
