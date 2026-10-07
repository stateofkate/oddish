"""The worker's retry decision must consult the CURRENT worker_jobs row, not
its claim-time snapshot: an operator capping max_attempts (or a reaper racing)
mid-attempt must bind at the very next failure decision.
"""

from __future__ import annotations

import asyncio

import pytest

import oddish.workers.queue.worker_job_single_job as wjs
from oddish.db import WorkerJobKind, WorkerJobStatus
from oddish.workers.jobs.registry import JobFailure, JobOutcome
from oddish.workers.queue.worker_job_single_job import record_outcome


class _FakeConnection:
    def __init__(self, current_attempts, current_max):
        self._row = {"attempts": current_attempts, "max_attempts": current_max}
        self.statements: list[str] = []

    async def fetchrow(self, sql, *args):
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        assert normalized.startswith("UPDATE worker_jobs")
        assert "attempts < max_attempts" in normalized
        assert "RETURNING status::text AS status" in normalized
        retryable = bool(args[4])
        status = (
            WorkerJobStatus.RETRYING.value
            if retryable and self._row["attempts"] < self._row["max_attempts"]
            else WorkerJobStatus.FAILED.value
        )
        return {**self._row, "status": status}

    async def execute(self, sql, *args):
        self.statements.append(" ".join(sql.split()))
        return "UPDATE 1"

    async def close(self):
        pass


def _run(outcome_kwargs, *, snapshot, current, monkeypatch):
    conn = _FakeConnection(*current)
    events = []

    async def _fake_open():
        return conn

    monkeypatch.setattr(wjs, "_open_connection", _fake_open)
    monkeypatch.setattr(
        wjs,
        "log_warning",
        lambda message, **attributes: events.append((message, attributes)),
    )
    monkeypatch.setattr(
        wjs,
        "log_error",
        lambda message, **attributes: events.append((message, attributes)),
    )
    status = asyncio.run(
        record_outcome(
            job_id="j1",
            worker_id="w1",
            outcome=JobOutcome(
                failure=JobFailure(error_message="boom", retryable=True)
            ),
            attempts=snapshot[0],
            max_attempts=snapshot[1],
        )
    )
    return status, conn, events


def test_mid_flight_cap_makes_failure_terminal(monkeypatch):
    # Claim snapshot said 1/6 (retry allowed); the row was capped to 1/1 while
    # the attempt ran. The failure must be TERMINAL, not retried.
    status, conn, events = _run(
        {}, snapshot=(1, 6), current=(1, 1), monkeypatch=monkeypatch
    )
    assert status == WorkerJobStatus.FAILED
    assert len(conn.statements) == 1
    assert "SELECT attempts, max_attempts" not in conn.statements[0]
    assert events[0][1]["attempt"] == 1
    assert events[0][1]["max_attempts"] == 1


def test_uncapped_failure_still_retries(monkeypatch):
    status, conn, events = _run(
        {}, snapshot=(1, 6), current=(1, 6), monkeypatch=monkeypatch
    )
    assert status == WorkerJobStatus.RETRYING
    assert len(conn.statements) == 1
    assert "SELECT attempts, max_attempts" not in conn.statements[0]
    assert events[0][1]["attempt"] == 1
    assert events[0][1]["max_attempts"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "concurrent_change",
    [
        "cap",
        "ownership",
        "none",
        "failed",
        "settling_failed",
        "success",
        "skipped",
        "mirror_error",
        "mirror_error_failed",
        "commit_error",
        "commit_error_failed",
    ],
)
async def test_outcome_observes_concurrent_database_update(
    monkeypatch, concurrent_change
):
    """Locked failure persistence must use committed job and trial state."""
    import os
    import uuid

    import asyncpg

    dsn = os.environ.get("ODDISH_DATABASE_URL")
    if not dsn:
        pytest.skip("ODDISH_DATABASE_URL not set")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    schema = "retry_outcome_" + uuid.uuid4().hex
    control = await asyncpg.connect(dsn)
    pending = None
    transaction = None
    try:
        await control.execute(f'CREATE SCHEMA "{schema}"')
        await control.execute(f'SET search_path TO "{schema}"')
        await control.execute("""
            CREATE TYPE worker_job_status AS ENUM ('RUNNING', 'RETRYING', 'FAILED');
            CREATE TABLE worker_jobs (
                id text PRIMARY KEY, status worker_job_status,
                attempts int, max_attempts int, error_message text,
                next_retry_at timestamptz, available_after timestamptz,
                finished_at timestamptz, payload jsonb,
                current_worker_id text, current_queue_slot int,
                modal_function_call_id text, external_id text, provider text
            );
            CREATE TABLE trials (
                id text PRIMARY KEY, status text, error_message text,
                next_retry_at timestamptz, current_worker_id text,
                current_queue_slot int, heartbeat_at timestamptz,
                finished_at timestamptz, deleted_at timestamptz,
                superseded_by_trial_id text
            );
            INSERT INTO worker_jobs VALUES (
                'job', 'RUNNING', 1, 6, NULL, NULL, NOW(), NULL,
                '{"registry_auth_enc":"test-only"}', 'worker', 1, 'call',
                'sandbox', 'modal'
            );
            INSERT INTO trials (id, status, current_worker_id)
                VALUES ('trial', 'RETRYING', 'worker');
        """)
        connection = await asyncpg.connect(dsn)
        await connection.execute(f'SET search_path TO "{schema}"')
        backend_pid = connection.get_server_pid()

        async def open_connection():
            return connection

        events = []
        monkeypatch.setattr(wjs, "_open_connection", open_connection)
        monkeypatch.setattr(wjs, "log_warning", lambda *a, **kw: events.append(kw))
        monkeypatch.setattr(wjs, "log_error", lambda *a, **kw: events.append(kw))
        terminal_status = {
            "failed": "FAILED",
            "settling_failed": "FAILED",
            "success": "SUCCESS",
            "skipped": "SKIPPED",
        }.get(concurrent_change)
        if concurrent_change.startswith(("mirror_error", "commit_error")):
            deferred = concurrent_change.startswith("commit_error")
            trigger = (
                "CREATE CONSTRAINT TRIGGER reject_update AFTER UPDATE ON trials DEFERRABLE INITIALLY DEFERRED"
                if deferred
                else "CREATE TRIGGER reject_update BEFORE UPDATE ON trials"
            )
            await control.execute(f"""
                CREATE FUNCTION reject_trial_update() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'trial mirror failed'; END;
                $$ LANGUAGE plpgsql;
                {trigger}
                FOR EACH ROW EXECUTE FUNCTION reject_trial_update();
            """)
        if concurrent_change in {"cap", "ownership", "settling_failed"}:
            transaction = control.transaction()
            await transaction.start()
            if concurrent_change == "cap":
                await control.execute("UPDATE worker_jobs SET max_attempts = 1")
            elif concurrent_change == "ownership":
                await control.execute(
                    "UPDATE worker_jobs SET current_worker_id = 'new-worker'"
                )
        if terminal_status:
            await control.execute(
                """UPDATE trials SET status = $1, finished_at = NOW(),
                   error_message = 'original provider failure', current_worker_id = NULL""",
                terminal_status,
            )
            settled_trial = dict(await control.fetchrow("SELECT * FROM trials"))
        pending = asyncio.create_task(
            record_outcome(
                job_id="job",
                worker_id="worker",
                outcome=JobOutcome.fail(
                    "provider overloaded",
                    retryable=not concurrent_change.endswith("_failed"),
                ),
                attempts=1,
                max_attempts=6,
                kind=WorkerJobKind.TRIAL,
                subject_table="trials",
                subject_id="trial",
            )
        )
        if transaction is not None:
            async with asyncio.timeout(5):
                while (
                    await control.fetchval(
                        "SELECT cardinality(pg_blocking_pids($1)) > 0",
                        backend_pid,
                    )
                    is not True
                ):
                    await asyncio.sleep(0.01)
            assert events == []
            await transaction.commit()
            transaction = None
        if concurrent_change.startswith(("mirror_error", "commit_error")):
            with pytest.raises(asyncpg.RaiseError, match="trial mirror failed"):
                await pending
            job = await control.fetchrow("SELECT * FROM worker_jobs")
            assert job["status"] == "RUNNING"
            assert job["current_worker_id"] == "worker"
            assert job["next_retry_at"] is None
            assert events == []
            return
        status = await pending
        job = await control.fetchrow("SELECT * FROM worker_jobs")
        trial = await control.fetchrow("SELECT * FROM trials")
        if terminal_status:
            assert status == WorkerJobStatus.FAILED
            assert job["status"] == "FAILED"
            assert dict(trial) == settled_trial
            assert job["next_retry_at"] is None
            assert job["finished_at"] is not None
            assert job["payload"] == "{}"
            assert len(events) == 1
            assert events[0]["metric"] == "worker_job_failed"
        elif concurrent_change == "cap":
            assert status == WorkerJobStatus.FAILED
            assert job["status"] == trial["status"] == "FAILED"
            assert job["finished_at"] is not None
            assert trial["finished_at"] is not None
            assert job["next_retry_at"] is None
            assert job["payload"] == "{}"
            assert events[0]["max_attempts"] == 1
        elif concurrent_change == "ownership":
            assert status is None
            assert job["status"] == "RUNNING"
            assert job["current_worker_id"] == "new-worker"
            assert trial["current_worker_id"] == "worker"
            assert events == []
        else:
            assert status == WorkerJobStatus.RETRYING
            assert job["status"] == trial["status"] == "RETRYING"
            assert job["next_retry_at"] == trial["next_retry_at"]
            assert job["next_retry_at"] is not None
            assert job["finished_at"] is None
            assert job["external_id"] is None
            assert len(events) == 1
    finally:
        if transaction is not None:
            await transaction.rollback()
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await control.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await control.close()
