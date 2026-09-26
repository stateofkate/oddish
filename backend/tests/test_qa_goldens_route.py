"""The /qa-goldens routes against the local Postgres: admin gating, org
scoping, status codes, and that writes commit.

    set -a && source .env && set +a && uv run pytest tests/test_qa_goldens_route.py
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routers.qa_goldens import router
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel, UserRole
from oddish.db import (
    ExperimentModel,
    QAGoldenCaseModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    get_session,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
    ),
]


@pytest_asyncio.fixture
async def seeded():
    """One org with a task, its version, an experiment and two failed solver
    trials. Yields (org, experiment name, trial ids); deletes it all after."""
    run = uuid.uuid4().hex[:8]
    org, task = f"org-{run}", f"golden-route-{run}"
    trials = [f"{task}-1", f"{task}-2"]
    async with get_session() as session:
        session.add(OrganizationModel(id=org, name=org, slug=org))
        await session.flush()
        experiment = ExperimentModel(name=f"exp-{run}", org_id=org)
        session.add(experiment)
        session.add(
            TaskModel(
                id=task,
                name=task,
                org_id=org,
                user="u",
                task_path="p",
                status=TaskStatus.COMPLETED,
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(id=f"{task}-v1", task_id=task, version=1, task_path="p")
        )
        await session.flush()
        for trial_id in trials:
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task,
                    task_version_id=f"{task}-v1",
                    experiment_id=experiment.id,
                    org_id=org,
                    agent="codex",
                    provider="local",
                    queue_key="q",
                    kind="agent",
                    status=TrialStatus.FAILED,
                    reward=0.0,
                )
            )
        await session.commit()
    try:
        yield org, experiment.name, trials
    finally:
        async with get_session() as session:
            for model in (QAGoldenCaseModel, TrialModel, ExperimentModel, TaskModel):
                await session.execute(
                    model.__table__.delete().where(model.__table__.c.org_id == org)
                )
            await session.execute(
                OrganizationModel.__table__.delete().where(OrganizationModel.id == org)
            )
            await session.commit()


def _client(auth: AuthContext) -> AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "auth",
    [
        AuthContext(
            method=AuthMethod.CLERK_JWT, org_id="org-1", user_role=UserRole.MEMBER
        ),
        AuthContext(method=AuthMethod.API_KEY, org_id="org-1", scope=APIKeyScope.TASKS),
    ],
)
async def test_every_golden_route_needs_an_org_admin(auth):
    async with _client(auth) as client:
        responses = [
            await client.get("/qa-goldens"),
            await client.get("/qa-goldens/trials", params={"review": "waiting"}),
            await client.post("/qa-goldens", json={}),
            await client.put("/qa-goldens/trials/t", json={}),
            await client.post("/qa-goldens/g/review"),
            await client.delete("/qa-goldens/g"),
        ]
    assert [r.status_code for r in responses] == [403] * 6


async def test_an_admin_labels_lists_reviews_and_retires_goldens(seeded):
    org, experiment, (first, second) = seeded
    admin = AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=org, user_id="ana", user_role=UserRole.ADMIN
    )
    full_key = AuthContext(
        method=AuthMethod.API_KEY, org_id=org, scope=APIKeyScope.FULL
    )
    async with _client(admin) as client:
        labeled = await client.put(
            f"/qa-goldens/trials/{first}", json={"classification": "BAD_FAILURE"}
        )
        added = await client.post(
            "/qa-goldens",
            json={
                "stage": "post_trial",
                "source": "labels.csv",
                "source_trial_id": second,
                "expected": {"classification": "GOOD_FAILURE"},
            },
        )
        duplicate = await client.post(
            "/qa-goldens",
            json={
                "stage": "post_trial",
                "source_trial_id": second,
                "expected": {"classification": "BAD_FAILURE"},
            },
        )
        bad_label = await client.put(
            f"/qa-goldens/trials/{first}", json={"classification": "FINE"}
        )
        missing = await client.put(
            "/qa-goldens/trials/nope", json={"classification": "BAD_FAILURE"}
        )
        listed = await client.get(
            "/qa-goldens", params=[("trial", first), ("trial", second)]
        )
        waiting = await client.get("/qa-goldens", params={"state": "waiting"})
        queue = await client.get(
            "/qa-goldens/trials", params={"experiment": experiment}
        )
        reviewed = await client.post(f"/qa-goldens/{added.json()['id']}/review")
    async with _client(full_key) as client:
        retired = await client.delete(f"/qa-goldens/{labeled.json()['id']}")
        again = await client.delete(f"/qa-goldens/{labeled.json()['id']}")
        after = await client.get("/qa-goldens")
        queues = [
            await client.get("/qa-goldens/trials", params=params)
            for params in ({}, {"review": "someday"}, {"experiment": "nope"})
        ]

    assert labeled.status_code == 200, labeled.text
    assert (labeled.json()["source_trial_id"], labeled.json()["state"]) == (
        first,
        "counted",
    )
    assert (added.json()["source"], added.json()["state"]) == ("labels.csv", "waiting")
    assert [duplicate.status_code, bad_label.status_code, missing.status_code] == [
        409,
        422,
        404,
    ]
    assert [g["source_trial_id"] for g in listed.json()] == [first, second]
    assert [g["source_trial_id"] for g in waiting.json()] == [second]
    assert [(t["trial_id"], t["group"]) for t in queue.json()["trials"]] == [
        (first, "labeled"),
        (second, "failed"),
    ]
    assert reviewed.json()["state"] == "counted"
    assert [retired.status_code, again.status_code] == [204, 404]
    assert [g["source_trial_id"] for g in after.json()] == [second]
    assert [r.status_code for r in queues] == [422, 422, 404]
