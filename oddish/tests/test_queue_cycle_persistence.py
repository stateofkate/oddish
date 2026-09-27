"""Real PostgreSQL checks for heartbeat/status and durable teardown intent."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import connection, utcnow
from oddish.workers.queue import pending_teardowns, runtime_status, tag_project_handler


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("ODDISH_DATABASE_URL not set")
    await connection.init_db()
    async with connection.engine.connect() as conn:
        transaction = await conn.begin()

        @asynccontextmanager
        async def get_session():
            async with AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                try:
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        monkeypatch.setattr(runtime_status, "get_session", get_session)
        monkeypatch.setattr(pending_teardowns, "get_session", get_session)
        try:
            yield get_session
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_only_completed_cycles_advance_last_success(sessions):
    await runtime_status.record_queue_runtime_status(
        "test-cycle", {"state": "completed", "spawned": 2}
    )
    first = await runtime_status.get_queue_runtime_status("test-cycle")
    assert first["last_success_at"]
    for state in ("deferred", "failed", "cancelled"):
        await runtime_status.record_queue_runtime_status(
            "test-cycle", {"state": state, "spawned": 0}
        )
        payload = await runtime_status.get_queue_runtime_status("test-cycle")
        assert payload["last_success_at"] == first["last_success_at"]
        assert payload["state"] == state
    await runtime_status.record_queue_runtime_status(
        "never-succeeded", {"state": "deferred"}
    )
    assert "last_success_at" not in await runtime_status.get_queue_runtime_status(
        "never-succeeded"
    )


@pytest.mark.asyncio
async def test_status_timeout_rolls_back_and_does_not_block_caller(monkeypatch):
    rolled_back = []
    session = AsyncMock()

    async def hang(*args):
        await asyncio.Event().wait()

    session.execute.side_effect = hang

    @asynccontextmanager
    async def get_session():
        try:
            yield session
        finally:
            rolled_back.append(True)

    monkeypatch.setattr(runtime_status, "get_session", get_session)
    monkeypatch.setattr(runtime_status, "STATUS_WRITE_TIMEOUT_SECONDS", 0.01)
    await asyncio.wait_for(
        runtime_status.record_queue_runtime_status("test", {"state": "completed"}), 0.5
    )
    assert rolled_back == [True]


@pytest.mark.asyncio
async def test_external_cancellation_of_status_write_propagates(monkeypatch):
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(runtime_status, "get_session", get_session)
    with pytest.raises(asyncio.CancelledError):
        await runtime_status.record_queue_runtime_status("test", {})


@pytest.mark.asyncio
async def test_teardown_intent_rolls_back_with_reap(sessions):
    with pytest.raises(RuntimeError):
        async with sessions() as session:
            await pending_teardowns.remember_pending_teardowns(
                session, {("daytona", "old")}, monitor_handles=set()
            )
            raise RuntimeError("reap transaction failed")
    assert (
        await runtime_status.get_queue_runtime_status(
            pending_teardowns.PENDING_TEARDOWNS
        )
        == {}
    )


@pytest.mark.asyncio
async def test_teardown_survives_cancellation_and_retries_from_storage(
    sessions, monkeypatch
):
    async with sessions() as session:
        await pending_teardowns.remember_pending_teardowns(
            session, {("daytona", "old")}, monitor_handles=set()
        )
    entered = asyncio.Event()

    async def stop(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pending_teardowns, "cancel_job_by_worker", stop)
    task = asyncio.create_task(pending_teardowns.retry_pending_teardowns())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await runtime_status.get_queue_runtime_status(
        pending_teardowns.PENDING_TEARDOWNS
    )
    assert stored["sandboxes"][0]["external_id"] == "old"
    assert stored["sandboxes"][0]["tries"] == 0
    confirmed = AsyncMock(return_value=True)
    monkeypatch.setattr(pending_teardowns, "cancel_job_by_worker", confirmed)
    assert await pending_teardowns.retry_pending_teardowns() == (1, 0)
    confirmed.assert_awaited_once_with("daytona", "old")
    assert await runtime_status.get_queue_runtime_status(
        pending_teardowns.PENDING_TEARDOWNS
    ) == {"sandboxes": []}


@pytest.mark.asyncio
async def test_failed_teardown_is_retained_and_registration_keeps_attempts(
    sessions, monkeypatch
):
    async with sessions() as session:
        await pending_teardowns.remember_pending_teardowns(
            session, {("ec2", "old")}, monitor_handles=set()
        )
    monkeypatch.setattr(
        pending_teardowns, "cancel_job_by_worker", AsyncMock(return_value=False)
    )
    assert await pending_teardowns.retry_pending_teardowns() == (0, 1)
    async with sessions() as session:
        await pending_teardowns.remember_pending_teardowns(
            session,
            {("ec2", "old"), ("modal", "monitor")},
            monitor_handles={("modal", "monitor")},
        )
    payload = await runtime_status.get_queue_runtime_status(
        pending_teardowns.PENDING_TEARDOWNS
    )
    by_id = {s["external_id"]: s for s in payload["sandboxes"]}
    assert by_id["old"]["tries"] == 1
    assert by_id["old"]["monitor_driven"] is False
    assert by_id["monitor"]["monitor_driven"] is True


@pytest.mark.asyncio
async def test_heartbeat_updates_real_column_and_preserves_payload(sessions):
    old = utcnow() - timedelta(hours=1)
    async with sessions() as session:
        await session.execute(
            text("""
            INSERT INTO worker_jobs (id, kind, status, queue_key, subject_id, payload, heartbeat_at, created_at, updated_at)
            VALUES ('tag-heartbeat', 'TAG_PROJECT', 'RUNNING', 'tags', 'exp-heartbeat',
                    '{"scope": "EXPERIMENT", "target_id": "exp-heartbeat"}'::jsonb, :old, NOW(), NOW())
        """),
            {"old": old},
        )
        await tag_project_handler._heartbeat_progress(
            session,
            target_id="exp-heartbeat",
            batch_size=50,
            progress={"last_processed": "task-049"},
        )
        row = (
            await session.execute(
                text(
                    "SELECT heartbeat_at, payload FROM worker_jobs WHERE id = 'tag-heartbeat'"
                )
            )
        ).one()
    assert row.heartbeat_at > old
    assert row.payload["scope"] == "EXPERIMENT"
    assert row.payload["last_batch_size"] == 50
    assert row.payload["tag_project_progress"] == {"last_processed": "task-049"}


@pytest.mark.asyncio
async def test_stale_job_batches_commit_progress_and_preserve_teardown(
    sessions, monkeypatch
):
    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus
    from oddish.workers.queue import cleanup

    monkeypatch.setattr(cleanup, "STALE_WORKER_BATCH_LIMIT", 2)
    async with sessions() as session:
        session.add_all(
            [
                WorkerJobModel(
                    id=f"batch-{i}",
                    kind=WorkerJobKind.TRIAL,
                    status=WorkerJobStatus.RUNNING,
                    queue_key="test",
                    attempts=1,
                    max_attempts=2,
                    provider="daytona",
                    external_id=f"sb-{i}",
                )
                for i in range(3)
            ]
        )
    for expected_count in (2, 1):
        async with sessions() as session:
            (
                retried,
                failed,
                _,
                targets,
                errors,
                releases,
            ) = await cleanup._reap_stale_worker_jobs(session, stale_after_minutes=15)
            assert (retried, failed, errors, releases) == (expected_count, 0, 0, [])
            await pending_teardowns.remember_pending_teardowns(
                session, targets, monitor_handles=set()
            )
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status::text, external_id FROM worker_jobs WHERE id LIKE 'batch-%'"
                )
            )
        ).all()
    assert rows == [("RETRYING", None)] * 3
    stored = await runtime_status.get_queue_runtime_status(
        pending_teardowns.PENDING_TEARDOWNS
    )
    assert {item["external_id"] for item in stored["sandboxes"]} == {
        "sb-0",
        "sb-1",
        "sb-2",
    }


@pytest.mark.asyncio
async def test_acknowledging_teardown_preserves_newly_registered_handle(
    sessions, monkeypatch
):
    async with sessions() as session:
        await pending_teardowns.remember_pending_teardowns(
            session, {("daytona", "old")}, monitor_handles=set()
        )

    async def stop(*args):
        # Provider call happens outside the record lock. Another cleanup can
        # commit intent here; acknowledging the old snapshot must preserve it.
        async with sessions() as session:
            await pending_teardowns.remember_pending_teardowns(
                session, {("daytona", "new")}, monitor_handles=set()
            )
        return True

    monkeypatch.setattr(pending_teardowns, "cancel_job_by_worker", stop)
    assert await pending_teardowns.retry_pending_teardowns() == (1, 0)
    stored = await runtime_status.get_queue_runtime_status(
        pending_teardowns.PENDING_TEARDOWNS
    )
    assert [entry["external_id"] for entry in stored["sandboxes"]] == ["new"]


@pytest.mark.asyncio
async def test_stale_batch_rotates_past_locked_domain_rows(sessions, monkeypatch):
    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus
    from oddish.workers.queue import cleanup

    monkeypatch.setattr(cleanup, "STALE_WORKER_BATCH_LIMIT", 2)
    async with sessions() as session:
        session.add_all(
            [
                WorkerJobModel(
                    id=f"rotate-{i}",
                    kind=WorkerJobKind.TRIAL,
                    status=WorkerJobStatus.RUNNING,
                    queue_key="test",
                    subject_id=f"trial-{i}",
                    attempts=1,
                    max_attempts=2,
                )
                for i in range(3)
            ]
        )

    async def mirror(session, row):
        if row["id"] in {"rotate-0", "rotate-1"}:
            raise cleanup._DomainRowLocked()
        return None

    monkeypatch.setattr(cleanup, "_mirror_stale_job_to_domain_row", mirror)
    for expected in (0, 1):
        async with sessions() as session:
            result = await cleanup._reap_stale_worker_jobs(
                session, stale_after_minutes=15
            )
            assert result[0] == expected
    async with sessions() as session:
        assert (
            await session.scalar(
                text("SELECT status::text FROM worker_jobs WHERE id='rotate-2'")
            )
            == "RETRYING"
        )
        assert (
            await session.scalar(
                text("SELECT status::text FROM worker_jobs WHERE id='rotate-0'")
            )
            == "RUNNING"
        )


@pytest.mark.asyncio
async def test_teardown_batches_rotate_failures_behind_untried_handles(
    sessions, monkeypatch
):
    monkeypatch.setattr(pending_teardowns, "TEARDOWN_BATCH_SIZE", 1)
    async with sessions() as session:
        await pending_teardowns.remember_pending_teardowns(
            session, {("ec2", "a"), ("ec2", "b")}, monitor_handles=set()
        )
    stop = AsyncMock(return_value=False)
    monkeypatch.setattr(pending_teardowns, "cancel_job_by_worker", stop)
    await pending_teardowns.retry_pending_teardowns()
    await pending_teardowns.retry_pending_teardowns()
    assert [call.args for call in stop.await_args_list] == [("ec2", "a"), ("ec2", "b")]
