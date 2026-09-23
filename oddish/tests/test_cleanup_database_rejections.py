"""A rejected stale job must not roll back or starve unrelated cleanup.

Temporary PostgreSQL tables exercise the actual reaper SQL and savepoints.
The trial mirror is reduced to a SQL update so failures can be injected at
claim, mirror, and old-sandbox-handle cleanup without touching real job rows.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from oddish.workers.queue import cleanup

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set"),
]


@pytest.mark.parametrize(
    ("rejection_phase", "sqlstate"),
    [("job", "P0001"), ("mirror", "23514"), ("handle", "P0001"), ("job", "42P01")],
)
async def test_rejected_job_is_atomic_and_does_not_stop_neighbors(
    monkeypatch, caplog, rejection_phase, sqlstate
):
    engine = create_async_engine(DB_URL, poolclass=NullPool)

    async def mirror(session, row):
        await session.execute(
            text("UPDATE cleanup_trial_mirrors SET status = :status WHERE id = :id"),
            {"id": row["subject_id"], "status": row["new_status"]},
        )
        return row["subject_id"] if row["new_status"] == "FAILED" else None

    monkeypatch.setattr(cleanup, "_mirror_stale_job_to_domain_row", mirror)
    try:
        async with engine.connect() as connection:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                await session.execute(
                    text("""
                    CREATE TEMP TABLE cleanup_trial_mirrors (
                        id text PRIMARY KEY, status text NOT NULL
                    )
                """)
                )
                await session.execute(
                    text("""
                    CREATE TYPE pg_temp.worker_job_status AS ENUM
                        ('RUNNING', 'RETRYING', 'FAILED')
                """)
                )
                await session.execute(
                    text("""
                    CREATE TEMP TABLE worker_jobs (
                        id text PRIMARY KEY,
                        status pg_temp.worker_job_status NOT NULL,
                        kind text NOT NULL DEFAULT 'TRIAL',
                        subject_table text NOT NULL DEFAULT 'trials',
                        subject_id text NOT NULL,
                        attempts int NOT NULL DEFAULT 1,
                        max_attempts int NOT NULL,
                        payload jsonb NOT NULL DEFAULT '{"registry_auth_enc":"test"}',
                        heartbeat_at timestamptz,
                        stale_reaped_at timestamptz,
                        finished_at timestamptz,
                        current_worker_id text DEFAULT 'worker',
                        current_queue_slot int DEFAULT 1,
                        modal_function_call_id text DEFAULT 'call',
                        error_message text,
                        heartbeat_failure_count int NOT NULL DEFAULT 0,
                        last_heartbeat_error text,
                        provider text DEFAULT 'modal',
                        external_id text DEFAULT 'sandbox'
                    )
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO worker_jobs (id, subject_id, status, max_attempts, external_id)
                    VALUES ('a', 'a', 'RUNNING', 2, 'sandbox-a'),
                           ('b', 'b', 'RUNNING', 2, 'sandbox-b'),
                           ('c', 'c', 'RUNNING', 1, 'sandbox-c')
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO cleanup_trial_mirrors VALUES
                        ('a', 'RUNNING'), ('b', 'RUNNING'), ('c', 'RUNNING')
                """)
                )
                # The middle job rejects either its first update, its trial
                # mirror, or its final handle clearing. SQLSTATE 42P01 models
                # a systemic schema failure, which must still escape.
                table = (
                    "cleanup_trial_mirrors"
                    if rejection_phase == "mirror"
                    else "worker_jobs"
                )
                condition = "NEW.id = 'b'"
                if rejection_phase == "handle":
                    condition += " AND NEW.external_id IS NULL"
                await session.execute(
                    text(f"""
                    CREATE FUNCTION pg_temp.reject_cleanup() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        IF {condition} THEN
                            RAISE EXCEPTION 'Test admission rejected' USING ERRCODE = '{sqlstate}';
                        END IF;
                        RETURN NEW;
                    END $$
                """)
                )
                await session.execute(
                    text(f"""
                    CREATE TRIGGER reject_cleanup BEFORE UPDATE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_cleanup()
                """)
                )
                await session.commit()

                if sqlstate == "42P01":
                    with pytest.raises(DBAPIError) as raised:
                        await cleanup._reap_stale_worker_jobs(
                            session, stale_after_minutes=15
                        )
                    assert raised.value.orig.sqlstate == sqlstate
                    await session.rollback()
                    return

                (
                    retried,
                    failed,
                    trials,
                    targets,
                    errors,
                    _,
                ) = await cleanup._reap_stale_worker_jobs(
                    session, stale_after_minutes=15
                )
                await session.commit()

                assert (retried, failed, trials, errors) == (1, 1, ["c"], 1)
                assert targets == {("modal", "sandbox-a"), ("modal", "sandbox-c")}
                assert (
                    f"Stale worker job b rejected by database (SQLSTATE {sqlstate})"
                    in caplog.text
                )
                rows = (
                    await session.execute(
                        text("""
                    SELECT w.id, w.status::text, m.status, w.external_id,
                           w.current_worker_id, w.stale_reaped_at IS NOT NULL
                    FROM worker_jobs w JOIN cleanup_trial_mirrors m ON m.id=w.id
                    ORDER BY w.id
                """)
                    )
                ).all()
                assert rows == [
                    ("a", "RETRYING", "RETRYING", None, None, True),
                    ("b", "RUNNING", "RUNNING", "sandbox-b", "worker", False),
                    ("c", "FAILED", "FAILED", "sandbox-c", None, True),
                ]
    finally:
        await engine.dispose()
