"""Preview upgrades keep saved rows and initialize each Neon child only once."""

import asyncio
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import text


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set_branch(monkeypatch, *, branch_id="br-preview", parent_id="br-staging"):
    monkeypatch.setenv("NEON_BRANCH_ID", branch_id)
    monkeypatch.setenv("NEON_PARENT_BRANCH_ID", parent_id)
    monkeypatch.setenv("ODDISH_DATABASE_URL", "postgresql://stub")


@pytest.mark.parametrize(
    ("branch_id", "parent_id"),
    [("", "br-staging"), ("br-preview", ""), ("br-main", "br-main")],
)
def test_refuses_missing_identity_or_staging_before_db_work(
    monkeypatch, branch_id, parent_id
):
    mod = _load_bootstrap()
    _set_branch(monkeypatch, branch_id=branch_id, parent_id=parent_id)
    calls = []
    monkeypatch.setattr(mod, "_upgrade_and_verify", lambda url: calls.append(url))
    with pytest.raises(RuntimeError, match="different branches"):
        mod.main()
    assert calls == []


def test_runs_both_stacks_before_schema_check_and_initialization(monkeypatch):
    mod = _load_bootstrap()
    _set_branch(monkeypatch)
    calls = []

    async def snapshot(url):
        calls.append("snapshot")

    async def verify(url):
        calls.append("verify")

    async def initialize(url, branch_id, parent_id):
        assert branch_id == "br-preview"
        assert parent_id == "br-staging"
        calls.append("initialize")
        return True

    monkeypatch.setattr(
        mod, "_upgrade_head", lambda project: calls.append(project.name)
    )
    monkeypatch.setattr(mod, "_assert_staging_snapshot", snapshot)
    monkeypatch.setattr(mod, "_assert_model_schema", verify)
    monkeypatch.setattr(mod, "_initialize_clone", initialize)
    mod.main()
    assert calls == ["snapshot", "oddish", "backend", "verify", "initialize"]


@pytest.mark.parametrize("failure_stage", ["oddish", "backend", "schema"])
def test_upgrade_or_schema_failure_stops_without_cleanup_or_retry(
    monkeypatch, failure_stage
):
    mod = _load_bootstrap()
    _set_branch(monkeypatch)
    calls = []

    async def snapshot(url):
        calls.append("snapshot")

    def upgrade(project):
        calls.append(project.name)
        if project.name == failure_stage:
            raise RuntimeError("Can't locate revision identified by 'removed_revision'")

    async def verify(url):
        calls.append("schema")
        if failure_stage == "schema":
            raise RuntimeError(
                "preview schema is missing model objects: trials.new_column"
            )

    async def initialize(*args):
        calls.append("initialize")

    monkeypatch.setattr(mod, "_upgrade_head", upgrade)
    monkeypatch.setattr(mod, "_assert_staging_snapshot", snapshot)
    monkeypatch.setattr(mod, "_assert_model_schema", verify)
    monkeypatch.setattr(mod, "_initialize_clone", initialize)
    with pytest.raises(
        RuntimeError, match="no reset was run.*restore any missing revision"
    ):
        mod.main()
    assert "initialize" not in calls
    assert calls.count("oddish") == 1
    assert calls[-1] == failure_stage


def test_branch_url_is_normalized(monkeypatch):
    mod = _load_bootstrap()
    monkeypatch.setenv("ODDISH_DATABASE_URL", "postgresql://u:p@h/db")
    assert mod._branch_db_url() == "postgresql+asyncpg://u:p@h/db"


@pytest.fixture(scope="module")
def isolated_postgres(tmp_path_factory):
    """Use a fresh local cluster, never a developer or hosted database."""
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip(
            "isolated PostgreSQL tests need initdb and pg_ctl as a non-root user"
        )
    directory = tmp_path_factory.mktemp("preview-postgres")
    data = directory / "data"
    subprocess.run(
        [
            initdb,
            "-D",
            str(data),
            "--no-locale",
            "--encoding=UTF8",
            "--auth=trust",
            "--username=preview_test",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-l",
            str(directory / "postgres.log"),
            "-o",
            f"-F -h 127.0.0.1 -k '' -p {port}",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        yield f"postgresql+asyncpg://preview_test@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
        )


# The fixture includes persisted user data plus every runtime field that the
# one-time initialization changes. Native enums exercise the production SQL.
_TEST_SCHEMA = """
CREATE TYPE jobstatus AS ENUM ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING', 'SUCCESS', 'FAILED', 'SKIPPED');
CREATE TYPE taskstatus AS ENUM ('PENDING', 'RUNNING', 'ANALYZING', 'VERDICT_PENDING', 'COMPLETED', 'FAILED');
CREATE TYPE worker_job_status AS ENUM ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED', 'SUCCESS', 'FAILED', 'CANCELLED');
CREATE TABLE tasks (id text PRIMARY KEY, name text, status taskstatus, finished_at timestamptz, updated_at timestamptz, verdict jsonb, verdict_status jobstatus, verdict_error text, verdict_finished_at timestamptz, CHECK (verdict IS NULL OR verdict = 'null'::jsonb OR (verdict_status IS NOT NULL AND verdict_status <> 'FAILED')));
CREATE TABLE task_versions (id text PRIMARY KEY, pre_trial jsonb, pre_trial_status jobstatus, pre_trial_error text, pre_trial_finished_at timestamptz, updated_at timestamptz);
CREATE TABLE trials (id text PRIMARY KEY, task_version_id text, status jobstatus, result jsonb, harbor_stage text, finished_at timestamptz, updated_at timestamptz, error_message text, next_retry_at timestamptz, current_worker_id text, current_queue_slot integer, claimed_at timestamptz, heartbeat_at timestamptz, analysis_status jobstatus, analysis_error text, analysis_finished_at timestamptz);
CREATE TABLE worker_jobs (id text PRIMARY KEY, status worker_job_status, finished_at timestamptz, updated_at timestamptz, error_message text, next_retry_at timestamptz, current_worker_id text, current_queue_slot integer, modal_function_call_id text, external_id text, claimed_at timestamptz, heartbeat_at timestamptz, job_token_hash text, job_token_expires_at timestamptz, job_token_revoked_at timestamptz, monitor_token_hash text, monitor_token_expires_at timestamptz, monitor_token_revoked_at timestamptz, provider text, reroute_pending_teardown boolean NOT NULL DEFAULT false);
CREATE TABLE api_keys (id text PRIMARY KEY, is_active boolean, updated_at timestamptz);
CREATE TABLE sandbox_runs (id text PRIMARY KEY, state text, external_id text, terminated_at timestamptz, last_error text, updated_at timestamptz);
CREATE TABLE sandbox_capacity_leases (id text PRIMARY KEY);
CREATE TABLE model_request_leases (id text PRIMARY KEY);
CREATE TABLE model_request_pools (id text PRIMARY KEY);
CREATE TABLE queue_dispatch_state (id text PRIMARY KEY);
CREATE TABLE user_provider_keys (id text PRIMARY KEY, ciphertext bytea);
CREATE TABLE queue_slots (id text PRIMARY KEY);
CREATE TABLE queue_runtime_status (id text PRIMARY KEY);
CREATE TABLE trial_events (id text PRIMARY KEY);
CREATE TABLE submission_idempotency (id text PRIMARY KEY);
CREATE TABLE alembic_version_oddish (version_num text PRIMARY KEY);
CREATE TABLE alembic_version_backend (version_num text PRIMARY KEY);
CREATE TABLE _preview_test_summaries (task_version_id text PRIMARY KEY, refreshes integer NOT NULL DEFAULT 0);
INSERT INTO alembic_version_oddish VALUES ('staging-core-revision');
INSERT INTO alembic_version_backend VALUES ('staging-backend-revision');
INSERT INTO tasks (id, name, status, verdict_status) VALUES ('inherited-active', 'staging task', 'RUNNING', 'QUEUED'), ('inherited-complete', 'saved task', 'COMPLETED', 'SUCCESS');
INSERT INTO task_versions (id, pre_trial_status) VALUES ('inherited-active', 'RUNNING'), ('inherited-complete', 'SUCCESS');
INSERT INTO trials (id, status, analysis_status, result) VALUES ('inherited-active', 'RUNNING', 'QUEUED', NULL), ('inherited-complete', 'SUCCESS', 'SUCCESS', '{"reward":1}');
UPDATE trials SET task_version_id = id;
INSERT INTO _preview_test_summaries (task_version_id) VALUES ('inherited-active'), ('inherited-complete');
INSERT INTO worker_jobs (id, status, modal_function_call_id, external_id, job_token_hash) VALUES ('inherited-active', 'RUNNING', 'parent-call', 'parent-handle', 'parent-token'), ('inherited-complete', 'SUCCESS', NULL, NULL, NULL);
INSERT INTO api_keys (id, is_active) VALUES ('parent-key', true);
INSERT INTO sandbox_runs (id, state, external_id) VALUES ('parent-live', 'RUNNING', 'parent-sandbox'), ('parent-closed', 'TERMINATED', NULL);
INSERT INTO sandbox_capacity_leases VALUES ('parent-capacity');
INSERT INTO model_request_leases VALUES ('parent-request');
INSERT INTO model_request_pools VALUES ('parent-pool');
INSERT INTO queue_dispatch_state VALUES ('parent-dispatch');
INSERT INTO user_provider_keys (id, ciphertext) VALUES ('parent-key', 'copied-ciphertext');
INSERT INTO queue_slots VALUES ('inherited-lease');
INSERT INTO queue_runtime_status VALUES ('inherited-worker');
INSERT INTO trial_events VALUES ('inherited-event');
INSERT INTO submission_idempotency VALUES ('inherited-request');
"""


async def _run_sql(mod, url, sql):
    engine = mod._engine(url)
    try:
        async with engine.begin() as conn:
            for statement in sql.split(";"):
                if statement.strip():
                    await conn.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest.fixture
def preview_database(isolated_postgres, monkeypatch):
    mod = _load_bootstrap()
    url = isolated_postgres
    asyncio.run(
        _run_sql(
            mod, url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" + _TEST_SCHEMA
        )
    )
    # Use the real AsyncSession/outer transaction, with a small stand-in for
    # the core aggregate writer. Its own algorithms have separate core tests.
    summary_module = types.ModuleType("oddish.core.task_browse_summary")
    summary_module.batches = []

    async def refresh(session, version_ids):
        summary_module.batches.append(version_ids)
        await session.execute(
            text(
                "UPDATE _preview_test_summaries SET refreshes = refreshes + 1 "
                "WHERE task_version_id = ANY(CAST(:ids AS text[]))"
            ),
            {"ids": version_ids},
        )

    summary_module.refresh_task_browse_summaries = refresh
    monkeypatch.setitem(sys.modules, summary_module.__name__, summary_module)
    return mod, url


async def _snapshot(mod, url):
    engine = mod._engine(url)
    try:
        async with engine.connect() as conn:
            tables = (
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                        )
                    )
                )
                .scalars()
                .all()
            )
            snapshot = {}
            for table in tables:
                result = await conn.execute(
                    text(
                        f'SELECT to_jsonb(t) FROM public."{table}" t ORDER BY to_jsonb(t)::text'
                    )
                )
                snapshot[table] = result.scalars().all()
            return snapshot
    finally:
        await engine.dispose()


def test_first_run_stops_only_inherited_work_and_keeps_saved_data(preview_database):
    mod, url = preview_database
    asyncio.run(
        _run_sql(
            mod,
            url,
            """
            UPDATE worker_jobs
            SET provider = 'thunder', external_id = 'saved-sandbox',
                modal_function_call_id = 'saved-worker',
                job_token_hash = 'saved-job-token',
                job_token_expires_at = now() + interval '1 hour',
                monitor_token_hash = 'saved-monitor-token',
                monitor_token_expires_at = now() + interval '1 hour',
                reroute_pending_teardown = true;
            """,
        )
    )
    before = asyncio.run(_snapshot(mod, url))
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    after = asyncio.run(_snapshot(mod, url))
    for table in (
        "tasks",
        "trials",
        "worker_jobs",
    ):
        rows = {row["id"]: row for row in after[table]}
        assert rows["inherited-complete"] == next(
            row for row in before[table] if row["id"] == "inherited-complete"
        )
        assert rows["inherited-active"]["status"] == (
            "CANCELLED" if table == "worker_jobs" else "FAILED"
        )
    job = next(row for row in after["worker_jobs"] if row["id"] == "inherited-active")
    assert job["modal_function_call_id"] is None
    assert job["external_id"] is None
    assert job["job_token_hash"] is None
    assert job["job_token_revoked_at"] is not None
    assert job["monitor_token_hash"] is None
    assert job["monitor_token_expires_at"] is None
    assert job["monitor_token_revoked_at"] is not None
    assert job["provider"] is None
    assert job["reroute_pending_teardown"] is False
    assert all(row["external_id"] is None for row in after["sandbox_runs"])
    assert all(row["state"] == "TERMINATED" for row in after["sandbox_runs"])
    assert after["api_keys"][0]["is_active"] is False
    assert after["user_provider_keys"] == before["user_provider_keys"]
    for table in (
        "queue_slots",
        "sandbox_capacity_leases",
        "model_request_leases",
        "model_request_pools",
        "queue_dispatch_state",
        "queue_runtime_status",
        "trial_events",
        "submission_idempotency",
    ):
        assert after[table] == []
    assert after[mod.INITIALIZATION_TABLE][0]["branch_id"] == "br-preview"
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is False
    assert asyncio.run(_snapshot(mod, url)) == after


def test_clone_stops_paused_and_monitor_work_without_erasing_results(preview_database):
    mod, url = preview_database
    asyncio.run(
        _run_sql(
            mod,
            url,
            """
            INSERT INTO trials (id, status) VALUES ('paused-trial', 'PAUSED');
            UPDATE worker_jobs SET monitor_token_hash = 'parent-monitor'
                WHERE id = 'inherited-active';
            INSERT INTO worker_jobs (id, status, provider, external_id, reroute_pending_teardown)
                VALUES ('failed-with-handle', 'FAILED', 'thunder', 'parent-handle', true);
            UPDATE tasks SET verdict = '{"decision": "accepted"}'
                WHERE id = 'inherited-active';
            UPDATE task_versions SET pre_trial = '{"checks": "passed"}'
                WHERE id = 'inherited-active';
            """,
        )
    )
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    after = asyncio.run(_snapshot(mod, url))
    paused = next(row for row in after["trials"] if row["id"] == "paused-trial")
    assert paused["status"] == "FAILED"
    assert paused["harbor_stage"] == "cancelled"
    monitor = next(
        row for row in after["worker_jobs"] if row["id"] == "inherited-active"
    )
    assert monitor["monitor_token_hash"] is None
    assert monitor["monitor_token_revoked_at"] is not None
    failed = next(
        row for row in after["worker_jobs"] if row["id"] == "failed-with-handle"
    )
    assert failed["status"] == "FAILED"
    assert failed["external_id"] == "parent-handle"
    assert failed["provider"] == "thunder"
    assert failed["reroute_pending_teardown"] is True
    task = next(row for row in after["tasks"] if row["id"] == "inherited-active")
    assert task["verdict"] == {"decision": "accepted"}
    assert task["verdict_status"] == "SUCCESS"
    version = next(
        row for row in after["task_versions"] if row["id"] == "inherited-active"
    )
    assert version["pre_trial"] == {"checks": "passed"}
    assert version["pre_trial_status"] == "SUCCESS"


def test_repeat_deploy_and_new_migration_keep_preview_rows(
    monkeypatch, preview_database
):
    mod, url = preview_database
    _set_branch(monkeypatch)
    monkeypatch.setenv("ODDISH_DATABASE_URL", url)
    upgraded = []

    def upgrade(project):
        upgraded.append(project.name)
        # Represents a new pending migration on a later deploy.
        if len(upgraded) == 3:
            asyncio.run(
                _run_sql(mod, url, "ALTER TABLE tasks ADD COLUMN new_field text")
            )

    async def verified(url):
        pass

    monkeypatch.setattr(mod, "_upgrade_head", upgrade)
    monkeypatch.setattr(mod, "_assert_model_schema", verified)
    mod.main()
    asyncio.run(
        _run_sql(
            mod,
            url,
            """
        INSERT INTO tasks (id, name, status) VALUES ('preview-task', 'keep me', 'RUNNING');
        INSERT INTO worker_jobs (id, status) VALUES ('preview-job', 'RUNNING');
        INSERT INTO sandbox_runs (id, state, external_id) VALUES ('preview-sandbox', 'RUNNING', 'preview-handle');
        INSERT INTO sandbox_capacity_leases VALUES ('preview-capacity');
        INSERT INTO model_request_leases VALUES ('preview-request');
        INSERT INTO api_keys (id, is_active) VALUES ('preview-key', true);
        INSERT INTO queue_slots VALUES ('preview-lease');
        INSERT INTO trial_events VALUES ('preview-event');
        INSERT INTO submission_idempotency VALUES ('preview-request');
    """,
        )
    )
    before = asyncio.run(_snapshot(mod, url))
    mod.main()
    for row in before["tasks"]:
        row["new_field"] = None
    assert asyncio.run(_snapshot(mod, url)) == before
    assert upgraded == ["oddish", "backend", "oddish", "backend"]
    asyncio.run(
        _run_sql(
            mod,
            url,
            "UPDATE tasks SET new_field = 'saved value' WHERE id = 'preview-task'",
        )
    )
    before = asyncio.run(_snapshot(mod, url))
    mod.main()
    assert asyncio.run(_snapshot(mod, url)) == before


def test_inherited_parent_marker_does_not_skip_child_initialization(preview_database):
    mod, url = preview_database
    asyncio.run(
        _run_sql(
            mod,
            url,
            f"""
        CREATE TABLE {mod.INITIALIZATION_TABLE} (branch_id text PRIMARY KEY, parent_branch_id text NOT NULL, initialized_at timestamptz NOT NULL DEFAULT now());
        INSERT INTO {mod.INITIALIZATION_TABLE} (branch_id, parent_branch_id) VALUES ('br-staging', 'br-older');
    """,
        )
    )
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    after = asyncio.run(_snapshot(mod, url))
    assert {row["branch_id"] for row in after[mod.INITIALIZATION_TABLE]} == {
        "br-staging",
        "br-preview",
    }
    assert after["api_keys"][0]["is_active"] is False


def test_failed_initialization_rolls_back_data_and_marker_then_retry_works(
    monkeypatch, preview_database
):
    mod, url = preview_database
    before = asyncio.run(_snapshot(mod, url))
    statements = mod._CLONE_INITIALIZATION_SQL
    monkeypatch.setattr(
        mod, "_CLONE_INITIALIZATION_SQL", statements + ("SELECT 1 / 0",)
    )
    with pytest.raises(Exception, match="division by zero"):
        asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging"))
    assert asyncio.run(_snapshot(mod, url)) == before
    monkeypatch.setattr(mod, "_CLONE_INITIALIZATION_SQL", statements)
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is False


def test_unknown_revision_failure_keeps_records_and_does_not_initialize(
    monkeypatch, preview_database
):
    mod, url = preview_database
    _set_branch(monkeypatch)
    monkeypatch.setenv("ODDISH_DATABASE_URL", url)
    before = asyncio.run(_snapshot(mod, url))

    def fail(project):
        raise RuntimeError("Can't locate revision identified by 'removed_revision'")

    monkeypatch.setattr(mod, "_upgrade_head", fail)
    with pytest.raises(RuntimeError, match="no reset was run"):
        mod.main()
    assert asyncio.run(_snapshot(mod, url)) == before


@pytest.mark.parametrize(
    "make_incomplete",
    [
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public",
        "DROP TABLE tasks",
        "DELETE FROM alembic_version_oddish",
        "UPDATE alembic_version_backend SET version_num = ''",
    ],
)
def test_uninitialized_clone_stops_before_migrations_or_writes(
    monkeypatch, preview_database, make_incomplete
):
    mod, url = preview_database
    _set_branch(monkeypatch)
    monkeypatch.setenv("ODDISH_DATABASE_URL", url)
    asyncio.run(_run_sql(mod, url, make_incomplete))
    before = asyncio.run(_snapshot(mod, url))
    calls = []
    monkeypatch.setattr(mod, "_upgrade_and_verify", lambda url: calls.append("upgrade"))

    with pytest.raises(
        RuntimeError, match="Finish the local staging data move, then run Preview Reset"
    ):
        mod.main()

    assert calls == []
    assert asyncio.run(_snapshot(mod, url)) == before


def test_concurrent_initialization_runs_only_once(preview_database):
    mod, url = preview_database

    async def run_together():
        return await asyncio.gather(
            mod._initialize_clone(url, "br-preview", "br-staging"),
            mod._initialize_clone(url, "br-preview", "br-staging"),
        )

    assert sorted(asyncio.run(run_together())) == [False, True]
    assert sys.modules["oddish.core.task_browse_summary"].batches == [
        ["inherited-active"]
    ]


def test_first_setup_refreshes_only_stopped_trial_versions(preview_database):
    mod, url = preview_database
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    after = asyncio.run(_snapshot(mod, url))
    assert {
        row["task_version_id"]: row["refreshes"]
        for row in after["_preview_test_summaries"]
    } == {
        "inherited-active": 1,
        "inherited-complete": 0,
    }
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is False
    assert asyncio.run(_snapshot(mod, url)) == after
    assert sys.modules["oddish.core.task_browse_summary"].batches == [
        ["inherited-active"]
    ]


def test_summary_failure_rolls_back_trial_changes_summary_and_marker(
    monkeypatch, preview_database
):
    mod, url = preview_database
    before = asyncio.run(_snapshot(mod, url))
    summary_module = sys.modules["oddish.core.task_browse_summary"]
    refresh = summary_module.refresh_task_browse_summaries

    async def fail_after_write(session, version_ids):
        await refresh(session, version_ids)
        raise RuntimeError("summary failed")

    monkeypatch.setattr(
        summary_module, "refresh_task_browse_summaries", fail_after_write
    )
    with pytest.raises(RuntimeError, match="summary failed"):
        asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging"))
    assert asyncio.run(_snapshot(mod, url)) == before

    monkeypatch.setattr(summary_module, "refresh_task_browse_summaries", refresh)
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    after = asyncio.run(_snapshot(mod, url))
    assert sum(row["refreshes"] for row in after["_preview_test_summaries"]) == 1
    assert len(after[mod.INITIALIZATION_TABLE]) == 1


def test_setup_without_active_trials_skips_summary_work(preview_database):
    mod, url = preview_database
    asyncio.run(_run_sql(mod, url, "UPDATE trials SET status = 'SUCCESS'"))
    assert asyncio.run(mod._initialize_clone(url, "br-preview", "br-staging")) is True
    assert sys.modules["oddish.core.task_browse_summary"].batches == []
