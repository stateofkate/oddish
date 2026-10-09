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
    TaskVersionModel,
)
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PATH = "/tasks/task/versions/v1/reviews"


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
        PATH + "/rating", json={"score": 5, "expected_revision": 0}
    )
    assert response.status_code == 200, response.text
    auth.user_id = "bob"
    comment = {
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
        await client.put(PATH + "/rating", json={"score": 1, "expected_revision": 0})
    ).status_code == 200
    assert (await client.get(PATH)).json()["average_score"] == 3


@pytest.mark.asyncio
async def test_revision_clear_and_restore(review_api):
    client, _, _ = review_api
    data = {"score": 4, "expected_revision": 0}
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
async def test_tenant_identity_and_version_scope(review_api):
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
                json={"score": invalid, "expected_revision": 0},
            )
        ).status_code == 422
    assert (
        await client.put(
            PATH + "/rating",
            json={"score": 4, "expected_revision": 0, "reviewer_id": "bob"},
        )
    ).status_code == 422
    assert (
        await client.put(
            PATH + "/rating",
            json={
                "content_hash": "stale",
                "score": 4,
                "expected_revision": 0,
            },
        )
    ).status_code == 422
    async with sessions.begin() as s:
        await s.execute(
            update(TaskVersionModel)
            .where(TaskVersionModel.id == "v1")
            .values(content_hash="new")
        )
    assert (await client.get(PATH)).status_code == 200


@pytest.mark.asyncio
async def test_comment_edits_ownership_pagination_and_retries(review_api):
    client, auth, _ = review_api
    ids = []
    for i in range(3):
        response = await client.post(
            PATH + "/comments",
            json={
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
    edit = {"body": "Edited", "expected_revision": 1}
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
        json={"body": "Comment", "client_request_id": str(uuid4())},
    )
    comment_id = saved.json()["id"]
    auth.user_id = "bob"
    auth.user_role = UserRole.ADMIN
    assert (
        await client.patch(
            PATH + "/comments/" + comment_id,
            json={"body": None, "expected_revision": 1},
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
        await client.put(PATH + "/rating", json={"score": 3, "expected_revision": 0})
    ).status_code == 404


@pytest.mark.asyncio
async def test_rating_write_serializes_on_task_version(
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
            client.put(PATH + "/rating", json={"score": 5, "expected_revision": 0})
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
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_version_without_fingerprint_supports_comment_crud(review_api):
    client, _, _ = review_api
    assert (await client.get(PATH)).status_code == 200
    result = await client.post(
        PATH + "/comments",
        json={"body": "Initial feedback", "client_request_id": str(uuid4())},
    )
    assert result.status_code == 200
    comment_id = result.json()["id"]
    assert (
        await client.patch(
            PATH + "/comments/" + comment_id,
            json={"body": "Updated feedback", "expected_revision": 1},
        )
    ).status_code == 200
    assert (await client.get(PATH)).json()["comments"][0]["body"] == "Updated feedback"
    assert (
        await client.request(
            "DELETE", PATH + "/comments/" + comment_id, json={"expected_revision": 2}
        )
    ).status_code == 200
    assert (await client.get(PATH)).json()["comments"] == []


@pytest.mark.asyncio
async def test_single_review_save_requires_both_fields_and_is_atomic(review_api):
    client, _, _ = review_api
    payload = {
        "score": 4,
        "body": "Clear requirements",
        "expected_revision": 0,
        "client_request_id": str(uuid4()),
    }
    for missing in ("score", "body"):
        invalid = {key: value for key, value in payload.items() if key != missing}
        assert (await client.post(PATH, json=invalid)).status_code == 422
    assert (await client.post(PATH, json={**payload, "body": "  "})).status_code == 422
    assert (await client.post(PATH, json={**payload, "score": None})).status_code == 422
    response = await client.post(PATH, json=payload)
    assert response.status_code == 200, response.text
    original = (await client.get(PATH)).json()
    assert original["own_rating"]["score"] == 4
    assert original["comments"][0]["body"] == "Clear requirements"
    comment = original["comments"][0]
    edit = {
        **payload,
        "score": 2,
        "body": "Needs clearer requirements",
        "expected_revision": original["own_rating_revision"],
        "comment_id": comment["id"],
        "comment_revision": comment["revision"],
    }
    assert (
        await client.post(PATH, json={**edit, "comment_revision": 99})
    ).status_code == 409
    after_failure = (await client.get(PATH)).json()
    assert after_failure["own_rating"] == original["own_rating"]
    assert after_failure["comments"] == original["comments"]
    assert (await client.post(PATH, json=edit)).status_code == 200
    updated = (await client.get(PATH)).json()
    assert updated["own_rating"]["score"] == 2
    assert updated["comments"][0]["body"] == "Needs clearer requirements"
    assert len(updated["comments"]) == 1


@pytest.mark.asyncio
async def test_review_save_cannot_edit_another_reviewer_or_leave_a_rating(review_api):
    client, auth, _ = review_api
    payload = {
        "score": 4,
        "body": "Feedback",
        "expected_revision": 0,
        "client_request_id": str(uuid4()),
    }
    assert (await client.post(PATH, json=payload)).status_code == 200
    review = (await client.get(PATH)).json()
    auth.user_id = "bob"
    auth.user_role = UserRole.ADMIN
    edit = {**payload, "comment_id": review["comments"][0]["id"], "comment_revision": 1}
    assert (await client.post(PATH, json=edit)).status_code == 403
    assert (await client.get(PATH)).json()["own_rating"] is None
    failed = {
        **payload,
        "client_request_id": str(uuid4()),
        "comment_id": "missing",
        "comment_revision": 1,
    }
    assert (await client.post(PATH, json=failed)).status_code == 403
    assert (await client.get(PATH)).json()["rating_count"] == 1


@pytest.mark.asyncio
async def test_own_review_survives_pagination_and_atomic_delete_rolls_back(review_api):
    client, auth, _ = review_api
    saved = await client.post(
        PATH,
        json={
            "score": 3,
            "expected_revision": 0,
            "body": "My saved review",
            "client_request_id": str(uuid4()),
        },
    )
    assert saved.status_code == 200
    own_id = saved.json()["id"]
    auth.user_id = "bob"
    for index in range(3):
        assert (
            await client.post(
                PATH + "/comments",
                json={"body": f"Newer {index}", "client_request_id": str(uuid4())},
            )
        ).status_code == 200
    auth.user_id = "alice"
    page = (await client.get(PATH + "?limit=1")).json()
    assert page["comment_count"] == 4
    assert page["comments"][0]["id"] != own_id
    assert page["own_comment"]["id"] == own_id
    assert page["own_comment"]["body"] == "My saved review"
    assert (
        await client.request(
            "DELETE",
            PATH,
            json={"comment_id": own_id, "expected_revision": 1, "rating_revision": 0},
        )
    ).status_code == 409
    stale = await client.request(
        "DELETE",
        PATH,
        json={"comment_id": own_id, "expected_revision": 2, "rating_revision": 1},
    )
    assert stale.status_code == 409
    unchanged = (await client.get(PATH)).json()
    assert unchanged["own_rating"]["score"] == 3
    assert unchanged["own_comment"]["id"] == own_id
    auth.user_id = "bob"
    assert (
        await client.request(
            "DELETE",
            PATH,
            json={"comment_id": own_id, "expected_revision": 1, "rating_revision": 0},
        )
    ).status_code == 403
    auth.user_id = "alice"
    assert (
        await client.request(
            "DELETE",
            PATH,
            json={"comment_id": own_id, "expected_revision": 1, "rating_revision": 1},
        )
    ).status_code == 200
    deleted = (await client.get(PATH)).json()
    assert deleted["own_rating"] is None and deleted["own_comment"] is None
    assert deleted["comment_count"] == 3 and deleted["rating_count"] == 0


@pytest.mark.asyncio
async def test_combined_delete_uses_latest_rating_revision_with_history(review_api):
    from datetime import datetime, timezone

    from models import TaskReviewRatingModel

    client, _, sessions = review_api
    async with sessions.begin() as session:
        session.add(
            TaskReviewRatingModel(
                id="old-rating",
                org_id="org",
                task_version_id="v1",
                reviewer_id="alice",
                score=1,
                revision=99,
                updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                deleted_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
            )
        )
        session.add(
            TaskReviewRatingModel(
                id="current-rating",
                org_id="org",
                task_version_id="v1",
                reviewer_id="alice",
                score=3,
                revision=1,
            )
        )
    saved = await client.post(
        PATH,
        json={
            "score": 4,
            "expected_revision": 1,
            "body": "Current review",
            "client_request_id": str(uuid4()),
        },
    )
    assert saved.status_code == 200, saved.text
    current = (await client.get(PATH)).json()
    assert current["own_rating_revision"] == 2
    assert (
        await client.request(
            "DELETE",
            PATH,
            json={
                "comment_id": saved.json()["id"],
                "expected_revision": 1,
                "rating_revision": 2,
            },
        )
    ).status_code == 200
    assert (await client.get(PATH)).json()["own_rating"] is None
