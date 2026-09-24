"""Run the probe removal migration against disposable PostgreSQL databases.

ODDISH_TEST_DATABASE_URL must allow CREATE DATABASE. Each case owns a new
database because the production trigger functions use the public schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


_SCHEMA = """
CREATE TABLE tasks (
    id text PRIMARY KEY, run_analysis boolean NOT NULL DEFAULT false,
    run_probe boolean NOT NULL DEFAULT false, qa_change_count integer DEFAULT 0
);
CREATE TABLE task_versions (
    id text PRIMARY KEY, task_id text REFERENCES tasks, pre_trial_status text
);
CREATE TABLE trials (
    id text PRIMARY KEY, task_id text REFERENCES tasks, task_version_id text,
    created_at timestamptz DEFAULT now(), deleted_at timestamptz,
    superseded_by_trial_id text, idempotency_key text,
    is_probe boolean NOT NULL DEFAULT false
);
CREATE TABLE worker_jobs (id text PRIMARY KEY, subject_table text, subject_id text);
CREATE TABLE sandbox_runs (trial_id text, worker_job_id text);
CREATE TABLE modal_costs (trial_id text, worker_job_id text);
CREATE TABLE worker_resource_attempts (worker_job_id text);
CREATE TABLE model_request_leases (worker_job_id text);
CREATE TABLE analysis_costs (trial_id text);
CREATE TABLE verifier_costs (trial_id text);
CREATE TABLE feedback (trial_id text);
CREATE TABLE api_keys (bound_analysis_trial_id text);
CREATE TABLE skills (id text PRIMARY KEY);
CREATE TABLE skill_files (skill_id text REFERENCES skills);
CREATE TABLE documents (id text PRIMARY KEY);
CREATE TABLE task_version_model_metrics (id text PRIMARY KEY);
CREATE INDEX idx_trials_task_browse_preview ON trials (task_id)
    WHERE NOT is_probe;

CREATE FUNCTION record_qa_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.qa_change_count := OLD.qa_change_count + 1;
    RETURN NEW;
END;
$$;
CREATE TRIGGER record_qa_change BEFORE UPDATE OF run_analysis ON tasks
    FOR EACH ROW EXECUTE FUNCTION record_qa_change();

INSERT INTO tasks (id) VALUES ('ordinary'), ('private');
INSERT INTO task_versions (id, task_id)
    VALUES ('ordinary-version', 'ordinary'), ('private-version', 'private');
INSERT INTO trials (id, task_id) VALUES ('solver', 'ordinary');
INSERT INTO worker_jobs VALUES ('solver-job', 'trials', 'solver');
"""

# Small fixtures for the observed production dependencies and effects. These
# are deliberately separate from the migration: a column-only fix still fails
# the audit update and trial insert checks below.
_FORGE_RULES = """
CREATE FUNCTION forge_private_source_binding(text, text, text, boolean DEFAULT true)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM tasks
        WHERE id = $1 AND id = 'private' AND run_probe IS FALSE);
$$;
CREATE FUNCTION forge_arm_private_source_lifecycle(text, text)
RETURNS void LANGUAGE sql AS $$
    UPDATE tasks SET run_analysis = false, run_probe = false WHERE id = $1;
$$;
CREATE FUNCTION forge_pin_private_source_analysis()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id = 'private' THEN
        NEW.run_analysis := false;
        NEW.run_probe := false;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER forge_private_source_analysis
    BEFORE UPDATE OF run_analysis, run_probe ON public.tasks
    FOR EACH ROW EXECUTE FUNCTION forge_pin_private_source_analysis();

CREATE FUNCTION forge_skip_private_source_audit_cas()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.pre_trial_status IS NULL AND NEW.pre_trial_status = 'QUEUED'
        AND forge_private_source_binding(NEW.task_id, NEW.id, 'org') THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER forge_private_source_audit_cas
    BEFORE UPDATE OF pre_trial_status ON public.task_versions
    FOR EACH ROW EXECUTE FUNCTION forge_skip_private_source_audit_cas();

CREATE FUNCTION forge_trials_admission_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.task_id = 'private' THEN
        UPDATE tasks SET run_probe = false WHERE id = NEW.task_id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER forge_trials_admission BEFORE INSERT ON public.trials
    FOR EACH ROW EXECUTE FUNCTION forge_trials_admission_guard();
"""


@pytest_asyncio.fixture
async def migration_database():
    raw_url = os.environ.get("ODDISH_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    url = make_url(raw_url)
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_probe_removal_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    test_url = url.set(database=name)
    engine = create_async_engine(test_url.set(drivername="postgresql+asyncpg"))
    conn = None
    try:
        conn = await asyncpg.connect(
            test_url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        await conn.execute(_SCHEMA)
        yield conn, engine
    finally:
        if conn is not None:
            await conn.close()
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _upgrade(engine):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    migration = ScriptDirectory.from_config(config).get_revision("probe_removal_001")

    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.module.upgrade()

    async with engine.connect() as connection:
        await connection.run_sync(run)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["clean", "forge", "forge-partial"])
async def test_probe_removal_keeps_normal_qa_and_can_restart(migration_database, state):
    conn, engine = migration_database
    if state != "clean":
        await conn.execute(_FORGE_RULES)
        # Establish that the fixture actually pins the private task's QA off.
        assert (
            await conn.fetchval(
                "UPDATE tasks SET run_analysis = true WHERE id = 'private' "
                "RETURNING run_analysis"
            )
            is False
        )
    if state == "forge-partial":
        # The failed production run committed this drop before reaching tasks.
        await conn.execute("ALTER TABLE trials DROP COLUMN is_probe")
    else:
        await conn.execute(
            "INSERT INTO trials (id, task_id, is_probe) VALUES ('probe', 'ordinary', true);"
            "INSERT INTO worker_jobs VALUES ('probe-job', 'trials', 'probe');"
        )

    for attempt in range(2):
        task_state = await conn.fetch("SELECT id, run_analysis FROM tasks ORDER BY id")
        audit_state = await conn.fetch(
            "SELECT id, pre_trial_status FROM task_versions ORDER BY id"
        )
        await _upgrade(engine)
        assert (
            await conn.fetch("SELECT id, run_analysis FROM tasks ORDER BY id")
            == task_state
        )
        assert (
            await conn.fetch(
                "SELECT id, pre_trial_status FROM task_versions ORDER BY id"
            )
            == audit_state
        )
        assert not await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND "
            "((table_name = 'tasks' AND column_name = 'run_probe') OR "
            "(table_name = 'trials' AND column_name = 'is_probe'))"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM trials WHERE id = 'probe'") == 0
        )
        assert await conn.fetchval("SELECT count(*) FROM worker_jobs") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM trials WHERE id = 'solver'") == 1
        )
        assert (
            await conn.fetchval(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = 'idx_trials_task_browse_preview'::regclass"
            )
            is True
        )
        for table in (
            "skills",
            "skill_files",
            "documents",
            "task_version_model_metrics",
        ):
            assert await conn.fetchval("SELECT to_regclass($1)", table) is None
        for signature in (
            "forge_pin_private_source_analysis()",
            "forge_skip_private_source_audit_cas()",
            "forge_trials_admission_guard()",
            "forge_arm_private_source_lifecycle(text,text)",
            "forge_private_source_binding(text,text,text,boolean)",
        ):
            assert await conn.fetchval("SELECT to_regprocedure($1)", signature) is None
        assert [
            row["tgname"]
            for row in await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
            )
        ] == ["record_qa_change"]

        before = await conn.fetchval("SELECT sum(qa_change_count) FROM tasks")
        await conn.execute("UPDATE tasks SET run_analysis = true")
        assert await conn.fetchval("SELECT bool_and(run_analysis) FROM tasks") is True
        assert (
            await conn.fetchval("SELECT sum(qa_change_count) FROM tasks") == before + 2
        )
        await conn.execute("UPDATE task_versions SET pre_trial_status = NULL")
        await conn.execute("UPDATE task_versions SET pre_trial_status = 'QUEUED'")
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM task_versions WHERE pre_trial_status = 'QUEUED'"
            )
            == 2
        )
        await conn.executemany(
            "INSERT INTO trials (id, task_id) VALUES ($1, $2)",
            [(f"new-{task}-{attempt}", task) for task in ("ordinary", "private")],
        )
