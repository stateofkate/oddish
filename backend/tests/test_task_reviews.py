"""Internal review HTTP behavior against an isolated PostgreSQL database."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from api.routers import task_reviews
from auth import AuthContext, AuthMethod, require_auth
from fastapi import FastAPI
from models import OrganizationModel, UserModel, UserRole
from oddish.db.models import (
    Base,
    TaskModel,
    TaskVersionFingerprintModel,
    TaskVersionModel,
)
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PATH = "/tasks/task/versions/v1/reviews"
SNAPSHOT = {"content_hash": "a" * 64, "content_algorithm": "sha256"}


@pytest_asyncio.fixture
async def review_api(monkeypatch):
    if not os.environ.get("ODDISH_TEST_DATABASE_URL"):
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    url = make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_reviews_api_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg")
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add_all(
                [
                    OrganizationModel(id="org", name="Org", slug="org"),
                    OrganizationModel(id="other", name="Other", slug="other"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    UserModel(
                        id="alice",
                        org_id="org",
                        email="alice@example.test",
                        name="Alice",
                    ),
                    UserModel(
                        id="bob", org_id="org", email="bob@example.test", name="Bob"
                    ),
                ]
            )
            session.add(
                TaskModel(
                    id="task", name="task", user="test", task_path="/task", org_id="org"
                )
            )
            await session.flush()
            session.add(
                TaskVersionModel(
                    id="v1",
                    task_id="task",
                    version=1,
                    task_path="/task",
                    content_hash="client",
                )
            )
            await session.flush()
            session.add(
                TaskVersionFingerprintModel(
                    task_version_id="v1",
                    content_hash=SNAPSHOT["content_hash"],
                    algorithm="sha256",
                    client_content_hash="client",
                )
            )

        @asynccontextmanager
        async def read():
            async with sessions() as s:
                yield s

        @asynccontextmanager
        async def write():
            async with sessions.begin() as s:
                yield s

        monkeypatch.setattr(task_reviews, "get_read_session", read)
        monkeypatch.setattr(task_reviews, "get_session", write)
        app = FastAPI()
        app.include_router(task_reviews.router)
        auth = AuthContext(
            method=AuthMethod.CLERK_JWT,
            org_id="org",
            user_id="alice",
            user_role=UserRole.MEMBER,
        )
        app.dependency_overrides[require_auth] = lambda: auth
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, auth, sessions
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_ratings_and_comment_only_reviews_do_not_share_denominators(review_api):
    client, auth, _ = review_api
    empty = (await client.get(PATH)).json()
    assert empty["average_score"] is None
    assert empty["own_rating_revision"] == 0
    response = await client.put(
        PATH + "/rating", json={**SNAPSHOT, "score": 5, "expected_revision": 0}
    )
    assert response.status_code == 200, response.text
    auth.user_id = "bob"
    comment = {
        **SNAPSHOT,
        "body": " Needs clearer instructions ",
        "client_request_id": str(uuid4()),
    }
    response = await client.post(PATH + "/comments", json=comment)
    assert response.status_code == 200, response.text
    assert (
        await client.post(PATH + "/comments", json=comment)
    ).json() == response.json()
    page = (await client.get(PATH)).json()
    assert page["rating_count"] == 1 and page["average_score"] == 5
    assert len(page["comments"]) == 1
    assert page["comments"][0]["body"] == "Needs clearer instructions"
    assert page["comments"][0]["reviewer"]["name"] == "Bob"
    assert "email" not in str(page)
    assert (
        await client.put(
            PATH + "/rating", json={**SNAPSHOT, "score": 1, "expected_revision": 0}
        )
    ).status_code == 200
    assert (await client.get(PATH)).json()["average_score"] == 3


@pytest.mark.asyncio
async def test_revision_clear_and_restore(review_api):
    client, _, _ = review_api
    data = {**SNAPSHOT, "score": 4, "expected_revision": 0}
    assert (await client.put(PATH + "/rating", json=data)).status_code == 200
    assert (await client.put(PATH + "/rating", json=data)).status_code == 409
    assert (
        await client.put(
            PATH + "/rating", json={**data, "score": None, "expected_revision": 1}
        )
    ).status_code == 200
    page = (await client.get(PATH)).json()
    assert page["own_rating"] is None and page["own_rating_revision"] == 2
    assert page["average_score"] is None
    assert (
        await client.put(PATH + "/rating", json={**data, "expected_revision": 2})
    ).status_code == 200
    assert (await client.get(PATH)).json()["rating_count"] == 1


@pytest.mark.asyncio
async def test_tenant_identity_and_content_guards(review_api):
    client, auth, sessions = review_api
    auth.org_id = "other"
    assert (await client.get(PATH)).status_code == 404
    auth.org_id = "org"
    auth.method = AuthMethod.API_KEY
    assert (await client.get(PATH)).status_code == 403
    auth.method = AuthMethod.CLERK_JWT
    for invalid in (0, 6, True, "4"):
        assert (
            await client.put(
                PATH + "/rating",
                json={**SNAPSHOT, "score": invalid, "expected_revision": 0},
            )
        ).status_code == 422
    assert (
        await client.put(
            PATH + "/rating",
            json={**SNAPSHOT, "score": 4, "expected_revision": 0, "reviewer_id": "bob"},
        )
    ).status_code == 422
    assert (
        await client.put(
            PATH + "/rating",
            json={
                **SNAPSHOT,
                "content_hash": "stale",
                "score": 4,
                "expected_revision": 0,
            },
        )
    ).status_code == 409
    async with sessions.begin() as s:
        await s.execute(
            update(TaskVersionModel)
            .where(TaskVersionModel.id == "v1")
            .values(content_hash="new")
        )
    assert (await client.get(PATH)).status_code == 404


@pytest.mark.asyncio
async def test_comment_edits_ownership_pagination_and_retries(review_api):
    client, auth, _ = review_api
    ids = []
    for i in range(3):
        response = await client.post(
            PATH + "/comments",
            json={
                **SNAPSHOT,
                "body": f"Comment {i}",
                "client_request_id": str(uuid4()),
            },
        )
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    page = (await client.get(PATH, params={"limit": 2})).json()
    next_page = (
        await client.get(
            PATH,
            params={
                "limit": 2,
                "before": page["next_before"],
                "before_id": page["next_before_id"],
            },
        )
    ).json()
    assert len(page["comments"]) == 2 and len(next_page["comments"]) == 1
    assert len({c["id"] for c in page["comments"] + next_page["comments"]}) == 3
    auth.user_id = "bob"
    edit = {**SNAPSHOT, "body": "Edited", "expected_revision": 1}
    assert (
        await client.patch(PATH + "/comments/" + ids[0], json=edit)
    ).status_code == 409
    auth.user_id = "alice"
    assert (
        await client.patch(PATH + "/comments/" + ids[0], json=edit)
    ).status_code == 200
    assert (
        await client.patch(PATH + "/comments/" + ids[0], json=edit)
    ).status_code == 409
    assert (
        await client.patch(
            PATH + "/comments/" + ids[0],
            json={**edit, "body": None, "expected_revision": 2},
        )
    ).status_code == 200
    assert len((await client.get(PATH)).json()["comments"]) == 2


@pytest.mark.asyncio
async def test_source_trial_must_match_subject_and_org(review_api):
    from oddish.db.models import ExperimentModel, TrialModel

    client, _, sessions = review_api
    async with sessions.begin() as session:
        session.add(ExperimentModel(id="exp", name="Exp", org_id="org"))
        await session.flush()
        session.add(
            TrialModel(
                id="trial",
                name="trial",
                task_id="task",
                task_version_id="v1",
                experiment_id="exp",
                org_id="org",
                agent="model",
                provider="test",
                queue_key="test",
            )
        )
    data = {
        **SNAPSHOT,
        "body": "Observed in this trial",
        "client_request_id": str(uuid4()),
    }
    assert (
        await client.post(
            PATH + "/comments", json={**data, "source_trial_id": "missing"}
        )
    ).status_code == 422
    assert (
        await client.post(PATH + "/comments", json={**data, "source_trial_id": "trial"})
    ).status_code == 200
    async with sessions.begin() as session:
        await session.execute(
            update(TrialModel).where(TrialModel.id == "trial").values(org_id="other")
        )
    assert (
        await client.post(
            PATH + "/comments",
            json={
                **data,
                "client_request_id": str(uuid4()),
                "source_trial_id": "trial",
            },
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_admin_moderation_deleted_subject_and_invalid_cursor(review_api):
    from oddish.db import utcnow

    client, auth, sessions = review_api
    saved = await client.post(
        PATH + "/comments",
        json={**SNAPSHOT, "body": "Comment", "client_request_id": str(uuid4())},
    )
    comment_id = saved.json()["id"]
    auth.user_id = "bob"
    auth.user_role = UserRole.ADMIN
    assert (
        await client.patch(
            PATH + "/comments/" + comment_id,
            json={**SNAPSHOT, "body": None, "expected_revision": 1},
        )
    ).status_code == 200
    assert (await client.get(PATH)).json()["comments"] == []
    assert (await client.get(PATH, params={"before_id": comment_id})).status_code == 422
    assert (
        await client.get(
            PATH, params={"before_id": comment_id, "before": "2026-10-08T00:00:00"}
        )
    ).status_code == 422
    async with sessions.begin() as session:
        await session.execute(
            update(TaskModel).where(TaskModel.id == "task").values(deleted_at=utcnow())
        )
    assert (await client.get(PATH)).status_code == 404
    assert (
        await client.put(
            PATH + "/rating", json={**SNAPSHOT, "score": 3, "expected_revision": 0}
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_write_waits_for_version_overwrite_before_checking_fingerprint(
    review_api,
):
    import asyncio

    from sqlalchemy import select, text

    client, _, sessions = review_api
    async with sessions.begin() as overwrite:
        await overwrite.scalar(
            select(TaskVersionModel)
            .where(TaskVersionModel.id == "v1")
            .with_for_update()
        )
        blocker_pid = await overwrite.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(
            client.put(
                PATH + "/rating", json={**SNAPSHOT, "score": 5, "expected_revision": 0}
            )
        )
        try:
            async with sessions() as observer:
                # Inspect the lock manager directly: pg_stat_activity can retain
                # its first activity snapshot throughout this observer transaction.
                async with asyncio.timeout(5):
                    while True:
                        blocked = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT FROM pg_locks WHERE NOT granted "
                                "AND :blocker = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"blocker": blocker_pid},
                        )
                        if blocked:
                            break
                        assert not pending.done(), (
                            "review write bypassed the version lock"
                        )
                        await asyncio.sleep(0.02)
            await overwrite.execute(
                update(TaskVersionModel)
                .where(TaskVersionModel.id == "v1")
                .values(content_hash="replacement")
            )
        except BaseException:
            pending.cancel()
            raise
    response = await asyncio.wait_for(pending, timeout=5)
    assert response.status_code == 404
