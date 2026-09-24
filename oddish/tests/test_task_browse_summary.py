"""Task-browser summary locking and the statistics backfill."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oddish.db import (
    ExperimentModel,
    TaskBrowseSummaryModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)


async def _seed(session, trials: list[dict]) -> tuple[str, str]:
    """Create a task + version and the given trials. Returns (task, version)."""
    suffix = uuid.uuid4().hex[:8]
    task_id = f"task-{suffix}"
    version_id = f"tv-{suffix}"
    experiment_id = f"exp-{suffix}"

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-1"))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id="org-1",
            user="tester",
            task_path="s3://test-bucket/tvm",
            status=TaskStatus.COMPLETED,
        )
    )
    # No relationship() links these, so SQLAlchemy will not order the inserts
    # by the foreign key on its own.
    await session.flush()
    session.add(
        TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=1,
            task_path="s3://test-bucket/tvm",
        )
    )
    await session.flush()

    for index, spec in enumerate(trials):
        trial_id = f"{task_id}-{index}"
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id="org-1",
                agent=spec.pop("agent", "claude-code"),
                model=spec.pop("model", "claude-opus-4-8"),
                provider=spec.pop("provider", "anthropic"),
                queue_key="anthropic/claude-opus-4-8",
                status=spec.pop("status", TrialStatus.SUCCESS),
                **spec,
            )
        )
    await session.flush()
    return task_id, version_id


async def _row(session, version_id: str):
    return await session.get(TaskBrowseSummaryModel, version_id)


# --- backfill ---------------------------------------------------------------
#
# The backfill drives its own sessions and commits, so these use committed rows
# rather than the rolled-back `session` fixture and clean up after themselves.


@pytest.mark.asyncio
async def test_backfill_covers_versions_and_skips_empty_ones(session):
    """A version with no in-scope trials gets an empty row and must not stall the loop."""
    from oddish.core.backfill_task_version_model_metrics import backfill

    _, with_trials = await _seed(session, [{"reward": 1.0, "total_steps": 11}])
    _, without_trials = await _seed(session, [])
    await session.commit()

    try:
        processed = await backfill(batch=5)
        assert processed > 0

        assert (await _row(session, with_trials)).total_trials == 1
        assert (await _row(session, without_trials)).total_trials == 0
    finally:
        await session.rollback()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(session):
    """Re-running must not duplicate or change rows."""
    from sqlalchemy import func as sa_func

    from oddish.core.backfill_task_version_model_metrics import backfill

    await _seed(session, [{"reward": 1.0, "total_steps": 21}, {"reward": 0.0}])
    await session.commit()

    async def _count() -> int:
        return (
            await session.execute(
                select(sa_func.count()).select_from(TaskBrowseSummaryModel)
            )
        ).scalar_one()

    try:
        await backfill(batch=5)
        first = await _count()
        assert first > 0

        await backfill(batch=5)
        session.expire_all()
        assert await _count() == first
    finally:
        await session.rollback()


@pytest.mark.asyncio
async def test_recompute_takes_the_version_advisory_lock(session):
    """The refresh blocks while a second connection holds a version's lock."""
    import asyncio

    import oddish.db.connection as conn
    from oddish.core.task_browse_summary import refresh_task_browse_summaries
    from sqlalchemy import text as sa_text

    _, version_id = await _seed(session, [{"reward": 1.0, "total_steps": 12}])
    await session.commit()

    holder = conn.async_session_maker()
    try:
        # Same hash expression as refresh_task_browse_summaries.
        await holder.execute(
            sa_text(
                "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:v AS text), 0))"
            ),
            {"v": version_id},
        )

        blocked = conn.async_session_maker()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    refresh_task_browse_summaries(
                        blocked, [version_id, "another-version"]
                    ),
                    timeout=2.0,
                )
        finally:
            await blocked.rollback()
            await blocked.close()
    finally:
        await holder.rollback()
        await holder.close()
        await session.rollback()


@pytest.mark.asyncio
async def test_batch_locks_use_one_statement_and_release_on_rollback(session):
    from sqlalchemy import event, text

    import oddish.db.connection as conn
    from oddish.core.task_browse_summary import lock_task_version_metrics

    version_ids = [f"lock-test-{i:03}" for i in range(200)]
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(conn.engine.sync_engine, "before_cursor_execute", record)
    try:
        await lock_task_version_metrics(session, version_ids)
    finally:
        event.remove(conn.engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    query = text(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(version_id, 0)) "
        "FROM unnest(CAST(:ids AS text[])) AS versions(version_id)"
    )
    async with conn.async_session_maker() as contender:
        held = (await contender.execute(query, {"ids": version_ids})).scalars().all()
        assert held == [False] * 200
        await session.rollback()
        released = (
            (await contender.execute(query, {"ids": version_ids})).scalars().all()
        )
        assert released == [True] * 200


@pytest.mark.asyncio
@pytest.mark.parametrize("batch,limit", [(0, None), (-1, None), (10, -1)])
async def test_backfill_rejects_invalid_batch_bounds(batch, limit):
    from oddish.core.backfill_task_version_model_metrics import backfill

    with pytest.raises(ValueError, match="batch must be positive"):
        await backfill(batch=batch, limit=limit)
