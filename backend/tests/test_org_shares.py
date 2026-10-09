"""Org sharing against real Postgres: who can open shared experiments, tasks,
and deliveries, by link or id."""

import os
import uuid

import auth
import httpx
import pytest
import pytest_asyncio
from api.routers import org_shares, public, public_analysis
from auth.types import AuthContext, AuthMethod
from fastapi import Depends, FastAPI, Request
from sqlalchemy import func, select
from models import OrganizationModel, UserRole
from oddish.db import (
    JobStatus,
    CustomerModel,
    DeliveryModel,
    DeliveryTaskModel,
    ExperimentModel,
    OrgShareModel,
    TaskModel,
    TaskVersionModel,
    get_session,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
)


@pytest_asyncio.fixture
async def world(monkeypatch):
    """Operator org owning one public experiment, plus two customer orgs."""
    tag = uuid.uuid4().hex[:8]
    ids = {k: f"share_{k}_{tag}" for k in ("operator", "cursive", "other")}
    token = f"tok_{tag}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=ids["operator"],
                name="Abundant",
                slug=ids["operator"],
                execution_enabled=True,
            )
        )
        for key in ("cursive", "other"):
            session.add(
                OrganizationModel(
                    id=ids[key],
                    name=key,
                    slug=ids[key],
                    viewer_access=True,
                    org_type="customer",
                    # "other" may run work but is still not the operator.
                    execution_enabled=key == "other",
                )
            )
        session.add(
            ExperimentModel(
                id=f"exp_{tag}",
                name="Cursive sample",
                org_id=ids["operator"],
                public_token=token,
                is_public=True,
            )
        )
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", ids["operator"])

    # Bearer <org id> stands in for a Clerk token on every route.
    async def fake_auth(request: Request, authorization=None, *_args):
        if not authorization:
            return AuthContext(method=AuthMethod.ANONYMOUS)
        org_id = authorization.removeprefix("Bearer ")
        # "bound:<org>" is an analysis sandbox key tied to one trial.
        bound = org_id.startswith("bound:")
        return AuthContext(
            method=AuthMethod.API_KEY if bound else AuthMethod.CLERK_JWT,
            org_id=org_id.removeprefix("bound:"),
            user_role=UserRole.ADMIN,
            bound_analysis_trial_id="trial_qa" if bound else None,
        )

    real_get_auth_context = auth.get_auth_context
    # set_share_viewer calls it directly; routes reach it through Depends.
    monkeypatch.setattr(auth, "get_auth_context", fake_auth)
    app = FastAPI()
    share_viewer = [Depends(auth.set_share_viewer)]
    app.include_router(public.router, dependencies=share_viewer)
    app.include_router(public_analysis.router, dependencies=share_viewer)
    app.include_router(org_shares.router)

    async def override(request: Request):
        return await fake_auth(request, request.headers.get("authorization"))

    app.dependency_overrides[real_get_auth_context] = override
    app.dependency_overrides[fake_auth] = override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleWorld(client, ids, token, f"exp_{tag}")

    async with get_session() as session:
        await session.execute(
            OrgShareModel.__table__.delete().where(
                OrgShareModel.grantee_org_id.in_(list(ids.values()))
            )
        )
        await session.execute(
            DeliveryModel.__table__.delete().where(
                DeliveryModel.org_id == ids["operator"]
            )
        )
        await session.execute(
            CustomerModel.__table__.delete().where(
                CustomerModel.org_id == ids["operator"]
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.org_id == ids["operator"])
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.org_id == ids["operator"]
            )
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(
                OrganizationModel.id.in_(list(ids.values()))
            )
        )


class SimpleWorld:
    def __init__(self, client, ids, token, experiment_id):
        self.client, self.ids, self.token, self.experiment_id = (
            client,
            ids,
            token,
            experiment_id,
        )

    async def get(self, path, org=None):
        headers = {"Authorization": f"Bearer {self.ids[org]}"} if org else {}
        return await self.client.get(path, headers=headers)


@pytest.mark.asyncio
async def test_org_share_opens_by_id_and_never_changes_the_public_link(world):
    link = f"/public/experiments/{world.token}"
    by_id = f"/public/experiments/{world.experiment_id}"
    assert (await world.get(link)).status_code == 200
    assert (await world.get(by_id, "cursive")).status_code == 404

    shared = await world.client.post(
        "/org-shares",
        json={
            "grantee_org_id": world.ids["cursive"],
            "resource_id": world.experiment_id,
        },
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )
    assert shared.status_code == 200, shared.text
    assert [s["grantee_org_name"] for s in shared.json()] == ["cursive"]

    # The public link stays open to anyone; grantees read by id.
    assert (await world.get(link)).status_code == 200
    assert (await world.get(link, "other")).status_code == 200
    assert (await world.get(by_id, "cursive")).status_code == 200
    assert (await world.get(by_id, "operator")).status_code == 200
    assert (await world.get(by_id, "other")).status_code == 404
    assert (await world.get(by_id)).status_code == 404
    # Child routes go through the same lookup.
    task = f"{by_id}/tasks/missing"
    assert (await world.get(task)).status_code == 404
    assert (await world.get(task, "other")).status_code == 404

    incoming = await world.get("/org-shares/incoming", "cursive")
    assert incoming.status_code == 200, incoming.text
    assert [e["id"] for e in incoming.json()] == [world.experiment_id]
    assert (await world.get("/org-shares/incoming", "other")).json() == []

    revoked = await world.client.delete(
        f"/org-shares/{shared.json()[0]['id']}",
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )
    assert revoked.status_code == 200 and revoked.json() == []
    assert (await world.get(link)).status_code == 200
    assert (await world.get(by_id, "cursive")).status_code == 404
    assert (await world.get("/org-shares/incoming", "cursive")).json() == []


@pytest.mark.asyncio
async def test_only_customer_orgs_can_be_picked_or_shared_with(world):
    internal = f"share_internal_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=internal, name="Someone's org", slug=internal, execution_enabled=True
            )
        )
    try:
        listed = await world.get("/org-shares/orgs", "operator")
        ids = {o["id"] for o in listed.json()}
        assert {world.ids["cursive"], world.ids["other"]} <= ids
        assert internal not in ids
        refused = await world.client.post(
            "/org-shares",
            json={"grantee_org_id": internal, "resource_id": world.experiment_id},
            headers={"Authorization": f"Bearer {world.ids['operator']}"},
        )
        assert refused.status_code == 404, refused.text
    finally:
        async with get_session() as session:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == internal
                )
            )


@pytest.mark.asyncio
async def test_only_the_operator_org_can_share(world):
    response = await world.client.post(
        "/org-shares",
        json={"grantee_org_id": world.ids["other"], "resource_id": world.experiment_id},
        headers={"Authorization": f"Bearer {world.ids['other']}"},
    )
    assert response.status_code == 403, response.text
    assert "Operator access required" in response.text
    orgs = await world.get("/org-shares/orgs", "other")
    assert orgs.status_code == 403


async def _private_experiment(world) -> str:
    experiment_id = f"exp_p_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id, name="Private", org_id=world.ids["operator"]
            )
        )
    return experiment_id


async def _share(world, resource_type: str, resource_id: str) -> httpx.Response:
    return await world.client.post(
        "/org-shares",
        json={
            "grantee_org_id": world.ids["cursive"],
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )


@pytest.mark.asyncio
async def test_experiment_share_opens_by_id_with_qa_and_cost_switch(world):
    experiment_id = await _private_experiment(world)
    by_id = f"/public/experiments/{experiment_id}"
    assert (await world.get(by_id, "cursive")).status_code == 404

    shared = await _share(world, "experiment", experiment_id)
    assert shared.status_code == 200, shared.text
    share = shared.json()[0]
    opened = await world.get(by_id, "cursive")
    assert opened.status_code == 200, opened.text
    # Customers always see QA; costs stay hidden until switched on.
    assert opened.json()["show_qa"] is True
    assert opened.json()["show_costs"] is False
    assert (await world.get(f"{by_id}/tasks/missing", "cursive")).status_code == 404
    assert (await world.get(by_id, "other")).status_code == 404
    assert (await world.get(by_id)).status_code == 404
    assert (await world.get(by_id, "operator")).json()["show_costs"] is True
    # A QA sandbox key of the owning org never opens experiments by id.
    bound = await world.client.get(
        by_id, headers={"Authorization": f"Bearer bound:{world.ids['operator']}"}
    )
    assert bound.status_code == 404, bound.text
    incoming = (await world.get("/org-shares/incoming", "cursive")).json()
    assert [e["id"] for e in incoming] == [experiment_id]

    shown = await world.client.patch(
        f"/org-shares/{share['id']}",
        json={"show_costs": True},
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )
    assert shown.status_code == 200, shown.text
    assert (await world.get(by_id, "cursive")).json()["show_costs"] is True

    revoked = await world.client.delete(
        f"/org-shares/{share['id']}",
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )
    assert revoked.status_code == 200, revoked.text
    assert (await world.get(by_id, "cursive")).status_code == 404
    assert (await world.get("/org-shares/incoming", "cursive")).json() == []


@pytest.mark.asyncio
async def test_tag_shares_are_refused(world):
    assert (await _share(world, "tag", "any")).status_code == 422


async def _task(world, name: str) -> tuple[str, str]:
    """An operator task with one version; returns (task id, version id)."""
    task_id = f"task_{name}_{uuid.uuid4().hex[:8]}"
    version_id = f"{task_id}-v1"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name=name,
                user="test",
                task_path="/tmp/fake",
                org_id=world.ids["operator"],
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id, task_id=task_id, version=1, task_path="/tmp/fake"
            )
        )
        await session.flush()
        (await session.get(TaskModel, task_id)).current_version_id = version_id
    return task_id, version_id


@pytest.mark.asyncio
async def test_sample_run_count_includes_gathered_runs(world):
    from oddish.db import TrialModel, experiment_trials, task_experiments

    collection = await _private_experiment(world)
    home = await _private_experiment(world)
    task_id, version_id = await _task(world, "gathered")
    async with get_session() as session:
        session.add(
            TrialModel(
                id=f"{task_id}-run",
                name="run",
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=home,
                org_id=world.ids["operator"],
                agent="nop",
                provider="anthropic",
                queue_key="test",
            )
        )
        await session.flush()
        # The run lives in another experiment; the shared one gathers it,
        # linking its task as a collection does.
        await session.execute(
            task_experiments.insert().values(task_id=task_id, experiment_id=collection)
        )
        await session.execute(
            experiment_trials.insert().values(
                experiment_id=collection, trial_id=f"{task_id}-run"
            )
        )
    assert (await _share(world, "experiment", collection)).status_code == 200
    incoming = (await world.get("/org-shares/incoming", "cursive")).json()
    assert [(e["id"], e["task_count"], e["run_count"]) for e in incoming] == [
        (collection, 1, 1)
    ]


@pytest.mark.asyncio
async def test_sample_counts_match_the_sample_page(world):
    """Runs on an older version of a task are not on the sample page, so
    the Samples list leaves them out too."""
    from oddish.db import TrialModel, task_experiments

    sample = await _private_experiment(world)
    task_id, v1 = await _task(world, "two-versions")
    async with get_session() as session:
        session.add(
            TaskVersionModel(
                id=f"{task_id}-v2", task_id=task_id, version=2, task_path="/tmp/fake"
            )
        )
        await session.flush()
        (await session.get(TaskModel, task_id)).current_version_id = f"{task_id}-v2"
        for version_id in (v1, f"{task_id}-v2"):
            session.add(
                TrialModel(
                    id=f"{version_id}-run",
                    name="run",
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=sample,
                    org_id=world.ids["operator"],
                    agent="nop",
                    provider="anthropic",
                    queue_key="test",
                )
            )
        await session.execute(
            task_experiments.insert().values(task_id=task_id, experiment_id=sample)
        )
    assert (await _share(world, "experiment", sample)).status_code == 200
    incoming = (await world.get("/org-shares/incoming", "cursive")).json()
    assert [(e["task_count"], e["run_count"]) for e in incoming] == [(1, 1)]


@pytest.mark.asyncio
async def test_task_share_opens_the_task_alone(world):
    task_id, _ = await _task(world, "shared-task")
    direct = f"/public/experiments/direct/tasks/{task_id}"
    assert (await world.get(direct, "cursive")).status_code == 404

    assert (await _share(world, "task", task_id)).status_code == 200
    opened = await world.get(direct, "cursive")
    assert opened.status_code == 200, opened.text
    assert opened.json()["trials"] == []
    assert (await world.get(f"{direct}/qa", "cursive")).status_code == 200
    assert (await world.get(f"{direct}/trials", "cursive")).json() == []
    assert (await world.get(direct, "other")).status_code == 404
    assert (await world.get(direct)).status_code == 404
    tasks = (await world.get("/org-shares/incoming/tasks", "cursive")).json()
    assert [(t["id"], t["version"]) for t in tasks] == [(task_id, None)]


@pytest.mark.asyncio
async def test_finalized_delivery_shares_its_board_and_pinned_tasks(world):
    task_id, version_id = await _task(world, "delivered")
    hidden_id, _ = await _task(world, "hidden")
    unpinned_id, _ = await _task(world, "unpinned")
    delivery_id = f"del_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            DeliveryModel(id=delivery_id, name="Batch 1", org_id=world.ids["operator"])
        )
        await session.flush()
        session.add(DeliveryTaskModel(delivery_id=delivery_id, task_id=task_id))
        session.add(DeliveryTaskModel(delivery_id=delivery_id, task_id=unpinned_id))
        session.add(
            DeliveryTaskModel(
                delivery_id=delivery_id, task_id=hidden_id, is_visible=False
            )
        )
    assert (await _share(world, "delivery", delivery_id)).status_code == 200
    # Shared before it ships: nothing to see yet.
    assert (await world.get("/org-shares/incoming/tasks", "cursive")).json() == []
    direct = f"/public/experiments/direct/tasks/{task_id}"
    assert (await world.get(direct, "cursive")).status_code == 404

    async with get_session() as session:
        delivery = await session.get(DeliveryModel, delivery_id)
        delivery.status = "finalized"
        delivery.finalized_at = func.now()
        await session.execute(
            DeliveryTaskModel.__table__.update()
            .where(DeliveryTaskModel.task_id == task_id)
            .values(pinned_version_id=version_id)
        )
    tasks = (await world.get("/org-shares/incoming/tasks", "cursive")).json()
    assert [(t["id"], t["version"], t["delivery_name"]) for t in tasks] == [
        (task_id, 1, "Batch 1")
    ]
    assert (await world.get(direct, "cursive")).status_code == 200
    # A v2 made after the delivery: the customer still sees v1 and v1's QA.
    async with get_session() as session:
        (await session.get(TaskVersionModel, version_id)).verdict = {"v": 1}
        session.add(
            TaskVersionModel(
                id=f"{task_id}-v2",
                task_id=task_id,
                version=2,
                task_path="/tmp/fake",
                verdict={"v": 2},
            )
        )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = f"{task_id}-v2"
        task.verdict = {"v": 2}
        task.verdict_status = JobStatus.SUCCESS
    status = (await world.get(direct, "cursive")).json()
    assert status["current_version"] == 1
    assert status["verdict"] != {"v": 2}
    # Only the shipped version opens; later (or unnamed) versions stay hidden.
    assert (await world.get(f"{direct}/qa?version=1", "cursive")).status_code == 200
    assert (await world.get(f"{direct}/qa?version=2", "cursive")).status_code == 404
    assert (await world.get(f"{direct}/qa", "cursive")).status_code == 404
    hidden = f"/public/experiments/direct/tasks/{hidden_id}"
    assert (await world.get(hidden, "cursive")).status_code == 404
    # A row finalized without a pinned version grants nothing.
    unpinned = f"/public/experiments/direct/tasks/{unpinned_id}"
    assert (await world.get(unpinned, "cursive")).status_code == 404


@pytest.mark.asyncio
async def test_task_refs_hide_experiments_shared_with_other_orgs(world):
    from oddish.core.sharing.public import _public_experiment_refs
    from oddish.db import TaskModel, task_experiments

    operator, task_id = world.ids["operator"], f"task_{world.experiment_id}"
    other_exp = f"{world.experiment_id}_b"
    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=other_exp,
                name="Other customer eval",
                org_id=operator,
                public_token=f"{world.token}_b",
                is_public=True,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name="t",
                user="test",
                task_path="/tmp/fake",
                org_id=operator,
            )
        )
        await session.flush()
        for exp in (world.experiment_id, other_exp):
            await session.execute(
                task_experiments.insert().values(task_id=task_id, experiment_id=exp)
            )
        session.add(
            OrgShareModel(
                grantee_org_id=world.ids["other"],
                resource_type="experiment",
                resource_id=other_exp,
            )
        )
    try:
        async with get_session() as session:
            refs = await _public_experiment_refs(
                session, [task_id], world.experiment_id
            )
            assert [r[0] for r in refs[task_id]] == [world.experiment_id]
            refs = await _public_experiment_refs(session, [task_id], other_exp)
            assert {r[0] for r in refs[task_id]} == {world.experiment_id, other_exp}
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


@pytest.mark.asyncio
async def test_share_of_a_deleted_experiment_can_be_revoked(world):
    from oddish.db import task_experiments

    operator = {"Authorization": f"Bearer {world.ids['operator']}"}
    task_id, _ = await _task(world, "in-experiment")
    async with get_session() as session:
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id, experiment_id=world.experiment_id
            )
        )
    shared = await world.client.post(
        "/org-shares",
        json={
            "grantee_org_id": world.ids["cursive"],
            "resource_id": world.experiment_id,
        },
        headers=operator,
    )
    direct = f"/public/experiments/direct/tasks/{task_id}"
    assert (await world.get(direct, "cursive")).status_code == 200
    async with get_session() as session:
        await session.execute(
            ExperimentModel.__table__.update()
            .where(ExperimentModel.id == world.experiment_id)
            .values(deleted_at=func.now())
        )
    # Deleting the experiment stops sharing its tasks as well.
    assert (await world.get(direct, "cursive")).status_code == 404
    assert (await world.get("/org-shares/incoming/tasks", "cursive")).json() == []
    revoked = await world.client.delete(
        f"/org-shares/{shared.json()[0]['id']}", headers=operator
    )
    assert revoked.status_code == 200, revoked.text


@pytest.mark.asyncio
async def test_id_reads_do_not_reveal_the_public_token(world):
    await world.client.post(
        "/org-shares",
        json={
            "grantee_org_id": world.ids["cursive"],
            "resource_id": world.experiment_id,
        },
        headers={"Authorization": f"Bearer {world.ids['operator']}"},
    )
    info = await world.get(f"/public/experiments/{world.experiment_id}", "cursive")
    assert info.status_code == 200, info.text
    assert info.json()["public_token"] == world.experiment_id
    assert world.token not in info.text


@pytest.mark.asyncio
async def test_finalize_shares_with_the_mapped_customer_org(world):
    delivery_id = f"del_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        mapped = CustomerModel(
            org_id=world.ids["operator"],
            name=f"Cursive {delivery_id}",
            customer_org_id=world.ids["cursive"],
        )
        unmapped = CustomerModel(
            org_id=world.ids["operator"], name=f"GDM {delivery_id}"
        )
        session.add_all([mapped, unmapped])
        await session.flush()
        for customer in (mapped, unmapped, None):
            await org_shares.share_with_customer_org(
                session, customer and customer.id, delivery_id, None
            )
        # Finalizing twice keeps one share.
        await org_shares.share_with_customer_org(session, mapped.id, delivery_id, None)
        await session.commit()
        grantees = await session.scalars(
            select(OrgShareModel.grantee_org_id).where(
                OrgShareModel.resource_id == delivery_id
            )
        )
        assert list(grantees) == [world.ids["cursive"]]
