"""Execute identity reads locally and count SQL, including ORM relationship loads."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from api.routers import dashboard as dashboard_router
from auth import APIKeyScope, AuthContext, AuthMethod
from models import APIKeyModel, OrganizationModel, UserModel, UserRole


@pytest.fixture
def identity_db(monkeypatch):
    # SQLite executes these portable identity queries without a running server.
    # Keep all mapped columns so accidentally selecting full ORM accounts would
    # execute their relationship queries and fail the statement-count assertions.
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in (OrganizationModel, UserModel, APIKeyModel):
        Table(
            model.__tablename__,
            metadata,
            *[
                Column(
                    column.name,
                    JSON() if isinstance(column.type, JSONB) else column.type,
                    primary_key=column.primary_key,
                    nullable=True,
                )
                for column in model.__table__.columns
            ],
        )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            metadata.tables["organizations"].insert(),
            {"id": "org_current", "name": "Current", "is_active": True},
        )
        accounts = [
            ("z", "Zulu@example.com", None, None, True, "org_current"),
            ("a", "alpha@example.com", "Zulu Name", "zulu", True, "org_current"),
            ("b", "Beta@example.com", None, None, True, "org_current"),
            ("inactive", "aaa@example.com", "Inactive", None, False, "org_current"),
            ("foreign", "aaa@example.com", "Foreign", None, True, "org_other"),
            ("substring", "a-beta@example.com", None, None, True, "org_current"),
            (
                "wildcard",
                "percent%_literal@example.com",
                None,
                None,
                True,
                "org_current",
            ),
        ]
        conn.execute(
            metadata.tables["users"].insert(),
            [
                dict(
                    id=id,
                    email=email,
                    name=name,
                    github_username=handle,
                    is_active=active,
                    org_id=org,
                    role="member",
                )
                for id, email, name, handle, active, org in accounts
            ],
        )

    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with Session(engine) as session:
        # Production functions await execute; the local driver is synchronous.
        adapter = SimpleNamespace(execute=AsyncMock(side_effect=session.execute))

        @asynccontextmanager
        async def get_session():
            yield adapter

        monkeypatch.setattr(dashboard_router, "get_read_session", get_session)
        yield adapter, statements
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "limit", "expected"),
    [
        ("", 3, ["substring", "a", "b"]),
        ("", 25, ["substring", "a", "b", "wildcard", "z"]),
        ("BETA", 1, ["b"]),
        ("beta@EXAMPLE.com", 25, ["b", "substring"]),
        ("Zulu Name", 25, ["a"]),
        ("@zulu", 25, ["a"]),
        ("b", 1, ["b"]),
        ("%_", 25, ["wildcard"]),
        ("aaa@example.com", 25, []),
    ],
)
async def test_people_query_email_order_search_and_scope(
    identity_db, query, limit, expected
):
    _, statements = identity_db
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_current",
        user_id="a",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.READ,
    )
    response = await dashboard_router.search_people(auth=auth, q=query, limit=limit)
    assert [person.id for person in response.items] == expected
    assert all(person.display_name == person.email for person in response.items)
    assert len(statements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_id, expected_name, query_count",
    [("a", "Zulu Name", 1), ("inactive", "Inactive", 2)],
)
async def test_author_enrichment_does_not_load_account_relationships(
    identity_db, owner_id, expected_name, query_count
):
    session, statements = identity_db
    original = {"owner_user_id": owner_id, "author": {"name": "old", "source": "api"}}
    dashboard = {"experiments": [original]}
    await dashboard_router._enrich_experiment_authors(
        session, dashboard, org_id="org_current"
    )
    assert dashboard["experiments"][0]["author"] == {
        "name": expected_name,
        "source": "member",
    }
    assert original["author"]["name"] == "old"
    assert len(statements) == query_count
    assert all("FROM users" in statement for statement in statements)
