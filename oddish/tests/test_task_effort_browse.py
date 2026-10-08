"""Real database sorting must precede pagination and use the displayed cohort."""

import pytest
from oddish.core.endpoints.tasks_query import browse_tasks_core
from sqlalchemy import event
from test_exact_token_counts import db  # noqa: F401
from test_task_run_coverage import seed


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["steps", "tokens"])
async def test_effort_sort_pagination_selection_and_nulls(db, metric):  # noqa: F811
    engine, sessions = db
    async with sessions() as session:
        ids = []
        for value in (0, 100, None, 30):
            task, _, _ = await seed(
                session,
                [
                    {
                        "total_steps": value,
                        "input_tokens": value,
                        "output_tokens": value,
                    },
                    # Baselines and infrastructure must not change either average.
                    {
                        "agent": "oracle",
                        "total_steps": 9999,
                        "input_tokens": 9999,
                        "output_tokens": 9999,
                    },
                    {
                        "error_message": "SandboxError",
                        "total_steps": 9999,
                        "input_tokens": 9999,
                        "output_tokens": 9999,
                    },
                ],
                org="effort-org",
            )
            ids.append(task)
        await seed(
            session,
            [{"total_steps": 99999, "input_tokens": 99999, "output_tokens": 99999}],
            org="other-org",
        )
        for direction, expected in [
            ("asc", [ids[0], ids[3], ids[1], ids[2]]),
            ("desc", [ids[1], ids[3], ids[0], ids[2]]),
        ]:
            sort = f"avg_{metric}_{direction}"
            seen = []
            for offset in range(4):
                page = await browse_tasks_core(
                    session, org_id="effort-org", sort=sort, limit=1, offset=offset
                )
                assert len(page.items) == 1
                item = page.items[0]
                seen.append(item.id)
                count = getattr(item.effort, f"{metric}_trial_count")
                expected_value = (
                    getattr(item.effort, f"{metric}_sum") / count if count else None
                )
                assert item.sort_value == expected_value
                assert item.effort.completed_trials == 1
            assert seen == expected
            assert (
                await browse_tasks_core(
                    session, org_id="effort-org", sort=sort, ids_only=True
                )
                == expected
            )
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert (
                await browse_tasks_core(
                    session,
                    org_id="effort-org",
                    sort=f"avg_{metric}_desc",
                    count_only=True,
                )
                == 4
            )
            assert not any(
                "steps_sum" in sql or "tokens_sum" in sql for sql in statements
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["steps", "tokens"])
async def test_effort_sort_preserves_fractional_averages(db, metric):  # noqa: F811
    _, sessions = db
    async with sessions() as session:
        ids = []
        for values in ((20, 21), (20, 20, 22)):
            task, _, _ = await seed(
                session,
                [
                    {"total_steps": value, "input_tokens": value, "output_tokens": 0}
                    for value in values
                ],
                org="fractional-effort",
            )
            ids.append(task)
        for direction, expected in (("asc", ids), ("desc", ids[::-1])):
            result = await browse_tasks_core(
                session, org_id="fractional-effort", sort=f"avg_{metric}_{direction}"
            )
            assert [item.id for item in result.items] == expected
            for item in result.items:
                assert item.sort_value == pytest.approx(
                    getattr(item.effort, f"{metric}_sum")
                    / getattr(item.effort, f"{metric}_trial_count")
                )
                assert item.sort_value != 20


@pytest.mark.asyncio
async def test_effort_migration_is_resumable_and_matches_runtime(db):  # noqa: F811
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from oddish.db import TaskBrowseSummaryModel
    from sqlalchemy import select, text

    engine, sessions = db
    async with sessions.begin() as session:
        await seed(session, [{"total_steps": 7, "input_tokens": 0, "output_tokens": 0}])
        await seed(session, [])
        expected = dict(
            (
                await session.execute(
                    select(
                        TaskBrowseSummaryModel.task_version_id,
                        TaskBrowseSummaryModel.effort,
                    )
                )
            ).all()
        )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "alembic")
    )
    migration = (
        ScriptDirectory.from_config(config).get_revision("task_effort_001").module
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE task_version_browse_summaries DROP COLUMN effort")
        )

    def upgrade(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()

    for _ in range(2):
        async with engine.connect() as connection:
            await connection.run_sync(upgrade)
        async with sessions() as session:
            assert (
                dict(
                    (
                        await session.execute(
                            select(
                                TaskBrowseSummaryModel.task_version_id,
                                TaskBrowseSummaryModel.effort,
                            )
                        )
                    ).all()
                )
                == expected
            )
