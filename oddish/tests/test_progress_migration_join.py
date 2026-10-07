"""Upgrade either deployed migration branch without skipping its sibling."""

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_head", ["trial_progress_001", "task_instruction_hash_001"]
)
async def test_upgrade_from_either_deployed_head(prior_head):
    url_value = os.environ.get("ODDISH_TEST_DATABASE_URL")
    if not url_value:
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    url = make_url(url_value).set(drivername="postgresql")
    admin = await asyncpg.connect(url.render_as_string(hide_password=False))
    name = "test_progress_join_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    database_url = url.set(database=name).render_as_string(hide_password=False)
    conn = await asyncpg.connect(database_url)

    async def alembic(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            *args,
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "ODDISH_DATABASE_URL": url.set(
                    database=name, drivername="postgresql+asyncpg"
                ).render_as_string(hide_password=False),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()

    try:
        # Minimal schema at the common ancestor; only disposable test DBs are
        # stamped. The two branch migrations and merge run through real Alembic.
        await conn.execute("CREATE TABLE trials (id VARCHAR(160) PRIMARY KEY)")
        await conn.execute(
            "CREATE TABLE task_version_fingerprints (id TEXT PRIMARY KEY)"
        )
        await conn.execute("INSERT INTO trials VALUES ('retained-trial')")
        await conn.execute("INSERT INTO task_version_fingerprints VALUES ('retained')")
        await alembic("stamp", "task_hash_idx_001")
        await alembic("upgrade", prior_head)
        if prior_head == "trial_progress_001":
            await conn.execute(
                "INSERT INTO trial_progress_events "
                "(trial_id, attempt, event_key, kind, source_seq, payload) "
                "VALUES ('retained-trial', 1, 'event', 'env_info', 0, '{}')"
            )
        else:
            await conn.execute(
                "UPDATE task_version_fingerprints SET instruction_hash = 'abc'"
            )

        await alembic("upgrade", "head")
        await alembic("upgrade", "head")  # Deploy retries must be harmless.
        assert (
            await conn.fetchval("SELECT version_num FROM alembic_version_oddish")
            == "progress_hash_merge_001"
        )
        assert await conn.fetchval("SELECT count(*) FROM alembic_version_oddish") == 1
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM task_version_fingerprints WHERE id = 'retained'"
            )
            == 1
        )
        instruction_hash = await conn.fetchval(
            "SELECT instruction_hash FROM task_version_fingerprints"
        )
        assert instruction_hash == (
            "abc" if prior_head == "task_instruction_hash_001" else None
        )
        assert (
            await conn.fetchval(
                "SELECT to_regclass('idx_task_version_fingerprints_instruction_hash')"
            )
            is not None
        )
        assert await conn.fetchval("SELECT count(*) FROM trial_progress_events") == (
            1 if prior_head == "trial_progress_001" else 0
        )
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()
