"""Admin > Customers against real Postgres, with Clerk's API faked."""

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select
from oddish.db import CustomerModel, ExperimentModel, OrgShareModel, get_session

import auth
import provision_org
from api.routers import customer_orgs, org_shares
from auth.provisioning import sync_clerk_org
from auth.types import AuthContext, AuthMethod
from models import OrganizationModel, UserRole

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
)


@pytest_asyncio.fixture
async def env(monkeypatch):
    tag = uuid.uuid4().hex[:8]
    operator, runner = f"cust_op_{tag}", f"cust_run_{tag}"
    clerk_id = f"org_clerk_{tag}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=operator, name="Abundant", slug=operator, execution_enabled=True
            )
        )
        session.add(
            OrganizationModel(
                id=runner,
                name="Runner",
                slug=runner,
                clerk_org_id=f"{clerk_id}_run",
                execution_enabled=True,
            )
        )
        session.add(
            ExperimentModel(
                id=f"exp_{tag}",
                name="Sample",
                org_id=operator,
                public_token=f"tok_{tag}",
                is_public=True,
            )
        )
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", operator)

    domains, clerk_calls = [], []

    async def fake_create(name):
        return {"id": clerk_id, "name": name, "slug": f"cust-{tag}"}

    async def fake_domain(clerk_org_id, domain):
        domains.append((clerk_org_id, domain))

    async def fake_fetch(session, clerk_org_id):
        return await sync_clerk_org(session, clerk_org_id, None, None)

    clerk_org = {
        "id": clerk_id,
        "has_image": True,
        "image_url": "https://img.clerk.com/logo",
        "members_count": 3,
    }
    clerk_get = {
        "/organizations": {"data": [clerk_org]},
        f"/organizations/{clerk_id}": clerk_org,
        f"/organizations/{clerk_id}/memberships": {
            "data": [
                {
                    "role": "org:admin",
                    "created_at": 1,
                    "public_user_data": {
                        "user_id": "user_1",
                        "identifier": "cto@cursive.ai",
                        "first_name": "Ada",
                        "last_name": None,
                    },
                }
            ]
        },
        f"/organizations/{clerk_id}/invitations": {
            "data": [
                {"id": "inv_1", "email_address": "new@cursive.ai", "role": "org:member"}
            ]
        },
        f"/organizations/{clerk_id}/domains": {
            "data": [
                {
                    "id": "dom_1",
                    "name": "cursive.ai",
                    "enrollment_mode": "automatic_invitation",
                }
            ]
        },
    }

    async def fake_clerk(method, path, **kwargs):
        clerk_calls.append((method, path, kwargs))
        if method == "GET":
            return clerk_get.get(path, {})
        if method == "PATCH" and path == f"/organizations/{clerk_id}":
            return {"id": clerk_id, **kwargs["json"]}
        return {}

    monkeypatch.setattr(customer_orgs, "clerk_api", fake_clerk)
    monkeypatch.setattr(customer_orgs, "create_clerk_org", fake_create)
    monkeypatch.setattr(customer_orgs, "add_clerk_org_domain", fake_domain)
    monkeypatch.setattr(provision_org, "fetch_and_sync_clerk_org", fake_fetch)

    async def override(request: Request):
        org = request.headers.get("x-test-org")
        return AuthContext(
            method=AuthMethod.CLERK_JWT, org_id=org, user_role=UserRole.ADMIN
        )

    app = FastAPI()
    app.include_router(customer_orgs.router)
    app.include_router(org_shares.router)
    app.dependency_overrides[auth.get_auth_context] = override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-test-org": operator},
    ) as client:
        client.clerk_calls = clerk_calls
        yield client, runner, f"exp_{tag}", domains, monkeypatch

    async with get_session() as session:
        await session.execute(
            OrgShareModel.__table__.delete().where(
                OrgShareModel.resource_id == f"exp_{tag}"
            )
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == f"exp_{tag}")
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(
                OrganizationModel.id.in_([operator, runner])
                | (OrganizationModel.clerk_org_id == clerk_id)
            )
        )


@pytest.mark.asyncio
async def test_create_share_and_revoke_customer(env):
    client, runner, experiment_id, domains, _ = env
    bad = await client.post(
        "/admin/customer-orgs", json={"name": "Cursive", "domain": "not a domain"}
    )
    assert bad.status_code == 422

    created = await client.post(
        "/admin/customer-orgs", json={"name": "Cursive", "domain": "@Cursive.AI"}
    )
    assert created.status_code == 200, created.text
    org = created.json()
    assert org["access"] == "viewer" and org["share_count"] == 0
    assert domains[-1][1] == "cursive.ai"

    shared = await client.post(
        "/org-shares", json={"grantee_org_id": org["id"], "resource_id": experiment_id}
    )
    assert shared.status_code == 200, shared.text
    shares = await client.get(f"/admin/customer-orgs/{org['id']}/shares")
    assert [(s["resource_type"], s["label"]) for s in shares.json()] == [
        ("experiment", "Sample")
    ]
    async with get_session() as session:
        await session.execute(
            ExperimentModel.__table__.update()
            .where(ExperimentModel.id == experiment_id)
            .values(deleted_at=func.now())
        )
    deleted = await client.get(f"/admin/customer-orgs/{org['id']}/shares")
    assert [s["inactive"] for s in deleted.json()] == [True]
    async with get_session() as session:
        await session.execute(
            ExperimentModel.__table__.update()
            .where(ExperimentModel.id == experiment_id)
            .values(deleted_at=None)
        )
    seen = await client.get(f"/admin/customer-orgs/{org['id']}/experiments")
    assert [e["id"] for e in seen.json()] == [experiment_id]
    listed = {o["id"]: o for o in (await client.get("/admin/customer-orgs")).json()}
    # Only customer orgs are listed: the runner org is internal.
    assert runner not in listed
    assert listed[org["id"]]["share_count"] == 1
    assert listed[org["id"]]["members_count"] == 3
    assert listed[org["id"]]["image_url"] == "https://img.clerk.com/logo"

    removed = await client.put(
        f"/admin/customer-orgs/{org['id']}/access", json={"access": "none"}
    )
    assert removed.json()["access"] == "none", removed.text
    granted = await client.put(
        f"/admin/customer-orgs/{org['id']}/access", json={"access": "viewer"}
    )
    assert granted.json()["access"] == "viewer", granted.text


@pytest.mark.asyncio
async def test_link_delivery_customer_to_org(env):
    client, runner, _, _, _ = env
    created = await client.post(
        "/admin/customer-orgs", json={"name": "Cursive", "domain": "cursive.ai"}
    )
    org_id = created.json()["id"]
    operator = client.headers["x-test-org"]
    async with get_session() as session:
        customer = CustomerModel(org_id=operator, name=f"Cursive {org_id}")
        session.add(customer)
        await session.commit()
        customer_id = customer.id
    try:
        base = f"/admin/customer-orgs/{org_id}/delivery-customers/{customer_id}"
        assert (await client.put(base, json={"linked": True})).status_code == 200
        detail = (await client.get(f"/admin/customer-orgs/{org_id}")).json()
        assert {c["id"]: c["customer_org_id"] for c in detail["delivery_customers"]}[
            customer_id
        ] == org_id
        assert (await client.put(base, json={"linked": False})).status_code == 200
        async with get_session() as session:
            assert (
                await session.get(CustomerModel, customer_id)
            ).customer_org_id is None
        missing = f"/admin/customer-orgs/{org_id}/delivery-customers/nope"
        assert (await client.put(missing, json={"linked": True})).status_code == 404
    finally:
        async with get_session() as session:
            await session.execute(
                CustomerModel.__table__.delete().where(CustomerModel.id == customer_id)
            )


@pytest.mark.asyncio
async def test_domain_failure_still_reports_the_created_org(env):
    client, _, _, _, monkeypatch = env

    async def failing_domain(clerk_org_id, domain):
        raise HTTPException(status_code=502, detail="Clerk: domain taken")

    monkeypatch.setattr(customer_orgs, "add_clerk_org_domain", failing_domain)
    response = await client.post(
        "/admin/customer-orgs", json={"name": "Cursive", "domain": "cursive.ai"}
    )
    assert response.status_code == 502
    assert "Created Cursive" in response.text and "domain taken" in response.text


async def _make_customer(org_id: str) -> None:
    async with get_session() as session:
        (await session.get(OrganizationModel, org_id)).org_type = "customer"


@pytest.mark.asyncio
async def test_execution_needs_a_budget_and_non_operators_are_refused(env):
    client, runner, _, _, _ = env
    base = f"/admin/customer-orgs/{runner}/access"
    # An internal org can't be managed (or demoted) from Customers.
    internal = await client.put(base, json={"access": "viewer"})
    assert internal.status_code == 404, internal.text
    await _make_customer(runner)
    no_budget = await client.put(base, json={"access": "execution"})
    assert no_budget.status_code == 422, no_budget.text
    approved = await client.put(
        base, json={"access": "execution", "monthly_limit_usd": 50}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["access"] == "execution"
    assert approved.json()["monthly_limit_usd"] == 50
    assert approved.json()["month_spend_usd"] == 0
    viewer = await client.put(base, json={"access": "viewer"})
    assert viewer.json()["access"] == "viewer", viewer.text
    outsider = await client.get("/admin/customer-orgs", headers={"x-test-org": runner})
    assert outsider.status_code == 403


@pytest.mark.asyncio
async def test_members_invitations_domains_and_rename_go_to_clerk(env):
    client, _, _, _, _ = env
    org = (
        await client.post(
            "/admin/customer-orgs", json={"name": "Cursive", "domain": "cursive.ai"}
        )
    ).json()
    base = f"/admin/customer-orgs/{org['id']}"

    members = (await client.get(f"{base}/members")).json()
    assert members == [
        {
            "user_id": "user_1",
            "email": "cto@cursive.ai",
            "name": "Ada",
            "image_url": None,
            "role": "org:admin",
            "joined_at": 1,
        }
    ]
    assert (await client.get(f"{base}/invitations")).json()[0][
        "email"
    ] == "new@cursive.ai"
    assert (await client.get(f"{base}/domains")).json()[0]["name"] == "cursive.ai"
    assert (
        await client.patch(f"{base}/members/user_1", json={"role": "org:owner"})
    ).status_code == 422
    for response in (
        await client.patch(f"{base}/members/user_1", json={"role": "org:member"}),
        await client.delete(f"{base}/members/user_1"),
        await client.delete(f"{base}/invitations/inv_1"),
        await client.post(f"{base}/domains", json={"domain": "@Cursive.dev"}),
        await client.delete(f"{base}/domains/dom_1"),
    ):
        assert response.status_code == 200, response.text
    renamed = await client.patch(base, json={"name": "Cursive Labs"})
    assert renamed.json()["name"] == "Cursive Labs", renamed.text

    writes = [(m, p) for m, p, _ in client.clerk_calls if m != "GET"]
    clerk_base = writes[0][1].split("/memberships")[0]
    assert writes == [
        ("PATCH", f"{clerk_base}/memberships/user_1"),
        ("DELETE", f"{clerk_base}/memberships/user_1"),
        ("POST", f"{clerk_base}/invitations/inv_1/revoke"),
        ("DELETE", f"{clerk_base}/domains/dom_1"),
        ("PATCH", clerk_base),
    ]


@pytest.mark.asyncio
async def test_logo_invite_and_delete_go_to_clerk(env):
    client, runner, _, _, _ = env
    await _make_customer(runner)
    org = (
        await client.post(
            "/admin/customer-orgs", json={"name": "Cursive", "domain": "cursive.ai"}
        )
    ).json()
    base = f"/admin/customer-orgs/{org['id']}"

    logo = await client.post(
        f"{base}/logo",
        json={
            "filename": "logo.png",
            "content_type": "image/png",
            "data_base64": "cG5n",
        },
    )
    assert logo.status_code == 200, logo.text
    assert (
        await client.post(f"{base}/invitations", json={"email": "x"})
    ).status_code == 422
    invite = await client.post(f"{base}/invitations", json={"email": "cto@cursive.ai"})
    assert invite.status_code == 200, invite.text
    assert (await client.delete(f"/admin/customer-orgs/{runner}")).status_code == 409
    deleted = await client.delete(base)
    assert deleted.status_code == 200, deleted.text
    async with get_session() as session:
        gone = await session.scalar(
            select(OrganizationModel)
            .where(OrganizationModel.id == org["id"])
            .execution_options(include_deleted=True)
        )
        # Soft-deleted here, like the organization.deleted webhook does.
        assert gone.deleted_at is not None and not gone.is_active

    calls = [(m, p) for m, p, _ in client.clerk_calls if m != "GET"]
    org_clerk_id = calls[0][1].split("/")[2]
    assert calls == [
        ("PUT", f"/organizations/{org_clerk_id}/logo"),
        ("POST", f"/organizations/{org_clerk_id}/invitations"),
        ("DELETE", f"/organizations/{org_clerk_id}"),
    ]
    invite_body = client.clerk_calls[-2][2]["json"]
    assert invite_body == {"email_address": "cto@cursive.ai", "role": "org:member"}
    listed = [o["id"] for o in (await client.get("/admin/customer-orgs")).json()]
    assert org["id"] not in listed


@pytest.mark.asyncio
async def test_clerk_api_rejects_dot_segments(monkeypatch):
    from auth import provisioning

    monkeypatch.setattr(provisioning, "CLERK_SECRET_KEY", "sk_test")
    with pytest.raises(HTTPException) as exc:
        await provisioning.clerk_api("DELETE", "/organizations/org_x/memberships/..")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_orgs_that_ran_jobs_cannot_be_deleted_after_a_demotion(env):
    from oddish.db import TaskModel

    client, runner, _, _, _ = env
    await _make_customer(runner)
    task_id = f"task_{runner}"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name="t",
                user="test",
                task_path="/tmp/x",
                org_id=runner,
                deleted_at=func.now(),  # deleted tasks still count
            )
        )
    try:
        base = f"/admin/customer-orgs/{runner}"
        demoted = await client.put(f"{base}/access", json={"access": "none"})
        assert demoted.json()["access"] == "none", demoted.text
        refused = await client.delete(base)
        assert refused.status_code == 409, refused.text
        assert not [c for c in client.clerk_calls if c[0] == "DELETE"]
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
