"""Review migration constraints and rollback on isolated PostgreSQL tables."""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_review_migration_constraints_and_round_trip():
    if not os.environ.get("ODDISH_TEST_DATABASE_URL"):
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    path = Path(__file__).parents[1] / "alembic/versions/task_reviews_001.py"
    spec = importlib.util.spec_from_file_location("task_reviews_001", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    path = path.with_name("task_reviews_version_002.py")
    spec = importlib.util.spec_from_file_location("task_reviews_version_002", path)
    version_migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(version_migration)
    url = make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_reviews_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg")
    )

    def apply(conn, direction, module=migration):
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, direction)()

    try:
        async with engine.begin() as conn:
            for table, size in (
                ("organizations", 64),
                ("users", 64),
                ("task_versions", 160),
                ("trials", 160),
            ):
                await conn.execute(
                    text(f"CREATE TABLE {table} (id varchar({size}) PRIMARY KEY)")
                )
                await conn.execute(text(f"INSERT INTO {table} VALUES ('one')"))
            await conn.run_sync(apply, "upgrade")
            values = {"id": "first", "score": 5}
            rating = text(
                "INSERT INTO task_review_ratings (id, org_id, reviewer_id, task_version_id, content_hash, content_algorithm, score) VALUES (:id, 'one', 'one', 'one', 'hash', 'sha256', :score)"
            )
            await conn.execute(rating, values)
            for invalid in (
                {"id": "duplicate", "score": 4},
                {"id": "invalid", "score": 6},
            ):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(rating, invalid)
            # Soft-clearing a rating must not allow a second current row.
            await conn.execute(text("UPDATE task_review_ratings SET deleted_at=now()"))
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(rating, {"id": "second", "score": 1})
            comment = text(
                "INSERT INTO task_review_comments (id,org_id,reviewer_id,task_version_id,content_hash,content_algorithm,body,client_request_id,source_trial_id) VALUES (:id,'one','one','one','hash','sha256',:body,:request,'one')"
            )
            await conn.execute(
                comment, {"id": "comment", "body": "Feedback", "request": "request"}
            )
            for values in (
                {"id": "empty", "body": "   ", "request": "empty"},
                {"id": "dupe", "body": "Again", "request": "request"},
            ):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(comment, values)
            await conn.execute(
                text("UPDATE task_review_ratings SET updated_at='2023-01-01'")
            )
            for ident, stamp in (("older", "2024-01-01"), ("latest", "2025-01-01")):
                await conn.execute(
                    text(
                        "INSERT INTO task_review_ratings (id,org_id,reviewer_id,task_version_id,content_hash,content_algorithm,score,updated_at) "
                        "VALUES (:id,'one','one','one',:id,'sha256',3,:stamp)"
                    ),
                    {"id": ident, "stamp": datetime.fromisoformat(stamp)},
                )
            await conn.run_sync(apply, "upgrade", version_migration)
            assert (
                await conn.scalar(text("SELECT count(*) FROM task_review_ratings")) == 3
            )
            assert (
                await conn.scalar(
                    text("SELECT id FROM task_review_ratings WHERE deleted_at IS NULL")
                )
                == "latest"
            )
            assert (
                await conn.scalar(text("SELECT body FROM task_review_comments"))
                == "Feedback"
            )
            bare_rating = text(
                "INSERT INTO task_review_ratings (id,org_id,reviewer_id,task_version_id,score) VALUES ('new','one','one','one',4)"
            )
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(bare_rating)
            await conn.execute(
                text(
                    "UPDATE task_review_ratings SET deleted_at=now() WHERE id='latest'"
                )
            )
            await conn.execute(bare_rating)
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM task_review_ratings WHERE deleted_at IS NULL"
                    )
                )
                == 1
            )
            await conn.run_sync(apply, "downgrade", version_migration)
            await conn.execute(text("DELETE FROM trials WHERE id='one'"))
            assert (
                await conn.scalar(
                    text("SELECT source_trial_id FROM task_review_comments")
                )
                is None
            )
            await conn.execute(text("DELETE FROM task_versions WHERE id='one'"))
            assert (
                await conn.scalar(text("SELECT count(*) FROM task_review_comments"))
                == 0
            )
            assert (
                await conn.scalar(text("SELECT count(*) FROM task_review_ratings")) == 0
            )
            await conn.run_sync(apply, "downgrade")
            assert (
                await conn.scalar(text("SELECT to_regclass('task_review_ratings')"))
                is None
            )
            await conn.run_sync(apply, "upgrade")
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()
