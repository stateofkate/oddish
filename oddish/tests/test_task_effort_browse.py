"""Real database sorting must precede pagination and use the displayed cohort."""

import pytest
from oddish.core.endpoints.tasks_query import browse_tasks_core
from sqlalchemy import event
from test_exact_token_counts import db  # noqa: F401
from test_task_run_coverage import seed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metric",
    ["steps", "tokens", "uncached_input_tokens", "cached_input_tokens", "output_tokens"],
)
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
                        "input_tokens": value * 2 if value is not None else None,
                        "cache_tokens": value,
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
@pytest.mark.parametrize(
    "metric",
    ["steps", "tokens", "uncached_input_tokens", "cached_input_tokens", "output_tokens"],
)
async def test_effort_sort_preserves_fractional_averages(db, metric):  # noqa: F811
    _, sessions = db
    async with sessions() as session:
        ids = []
        for values in ((20, 21), (20, 20, 22)):
            task, _, _ = await seed(
                session,
                [
                    {
                        "total_steps": value,
                        "input_tokens": value * 2,
                        "cache_tokens": value,
                        "output_tokens": value,
                    }
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
@pytest.mark.parametrize("fresh_column", [False, True])
async def test_effort_migration_is_resumable_and_matches_runtime(db, fresh_column):  # noqa: F811
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
        ScriptDirectory.from_config(config).get_revision("task_effort_tokens_001").module
    )
    async with engine.begin() as connection:
        if fresh_column:
            await connection.execute(text("DROP TRIGGER task_effort_z_cost_compat ON task_version_browse_summaries"))
        await connection.execute(
            text(
                "ALTER TABLE task_version_browse_summaries DROP COLUMN effort"
                if fresh_column
                else "UPDATE task_version_browse_summaries SET effort = effort "
                "- 'uncached_input_tokens_trial_count'"
            )
        )

    def upgrade(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            if fresh_column:
                original = ScriptDirectory.from_config(config).get_revision("task_effort_001").module
                original.upgrade()
            migration.upgrade()
            ScriptDirectory.from_config(config).get_revision("task_cost_summary_001").module.upgrade()
            ScriptDirectory.from_config(config).get_revision("task_cost_settled_002").module.upgrade()

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input,cache,output,expected",
    [
        (100, None, 20, (0, 0, 0, 0, 20, 1)),
        (None, 50, None, (0, 0, 50, 1, 0, 0)),
        (100, 0, 0, (100, 1, 0, 1, 0, 1)),
        (100, 101, 20, (0, 0, 101, 1, 20, 1)),
        (100, -1, -1, (0, 0, 0, 0, 0, 0)),
        (-1, 0, 20, (0, 0, 0, 1, 20, 1)),
        (5_714_326_425, 4_000_000_001, 20, (1_714_326_424, 1, 4_000_000_001, 1, 20, 1)),
    ],
)
async def test_breakdown_recorded_values_and_independent_denominators(
    db, input, cache, output, expected,  # noqa: F811
):
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from oddish.core.endpoints.task_open_queries import AGGREGATE_SQL
    from oddish.db import TaskBrowseSummaryModel
    from oddish.schemas import TaskEffortSummary
    from sqlalchemy import text

    _, sessions = db
    async with sessions() as session:
        task, version, _ = await seed(
            session,
            [{
                "input_tokens": input, "cache_tokens": cache, "output_tokens": output,
                "cache_write_tokens": 30,
            }],
        )
        saved = await session.get(TaskBrowseSummaryModel, version)
        effort = TaskEffortSummary.model_validate(saved.effort)
        fields = [
            f"{metric}_{suffix}"
            for metric in ("uncached_input_tokens", "cached_input_tokens", "output_tokens")
            for suffix in ("sum", "trial_count")
        ]
        assert tuple(getattr(effort, field) for field in fields) == expected
        row = (
            await session.execute(AGGREGATE_SQL, {
                "task_id": task, "org_id": None,
                "version_id": version, "current_version_id": version,
            })
        ).mappings().one()
        assert tuple(row["groups"][0][f"effort_{field}"] for field in fields) == expected
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[1] / "alembic")
        )
        migration = (
            ScriptDirectory.from_config(config).get_revision("task_effort_tokens_001").module
        )
        await session.execute(text(migration._BACKFILL), {"ids": [version]})
        await session.refresh(saved)
        assert TaskEffortSummary.model_validate(saved.effort) == effort


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_old_summary_writers_keep_breakdown_current(db, empty):  # noqa: F811
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from oddish.core.endpoints.task_open_queries import AGGREGATE_SQL
    from oddish.db import TaskBrowseSummaryModel, TrialModel
    from sqlalchemy import delete, select, text, update

    engine, sessions = db
    async with sessions.begin() as session:
        task, version, _ = await seed(session, [] if empty else [
            {"input_tokens": 100, "cache_tokens": 40, "output_tokens": 20, "total_steps": 7},
            {"agent": "oracle", "input_tokens": 999, "cache_tokens": 999},
            {"kind": "qa", "input_tokens": 999, "cache_tokens": 999},
            {"error_message": "SandboxError", "input_tokens": 999, "cache_tokens": 999},
        ])
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    migration = ScriptDirectory.from_config(config).get_revision("task_effort_tokens_001").module

    def migrate(connection, downgrade=False):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            (migration.downgrade if downgrade else migration.upgrade)()

    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    async with sessions.begin() as session:
        if not empty:
            await session.execute(update(TrialModel).where(TrialModel.id == f"{task}-0").values(
                input_tokens=5_714_326_425, cache_tokens=4_000_000_001, output_tokens=55,
            ))
        old = {"completed_trials": 1, "steps_sum": 17, "steps_trial_count": 1,
               "tokens_sum": 333, "tokens_trial_count": 1}
        # Old finalizers replace the whole JSON after updating trial measurements.
        await session.execute(update(TaskBrowseSummaryModel).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ).values(effort=old))
        effort = (await session.execute(select(TaskBrowseSummaryModel.effort).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ))).scalar_one()
        assert {key: effort[key] for key in old} == old
        fields = set(effort) - set(old) - {"cost"}
        assert len(fields) == 6
        row = (await session.execute(AGGREGATE_SQL, {
            "task_id": task, "org_id": None,
            "version_id": version, "current_version_id": version,
        })).mappings().one()
        for field in fields:
            assert effort[field] == sum(group[f"effort_{field}"] for group in row["groups"] or [])
        if not empty:
            assert effort["uncached_input_tokens_sum"] == 1_714_326_424
        # A null snapshot has no supplied base fields; reconstruct the complete
        # summary so the marker cannot hide a partial historical backfill.
        await session.execute(update(TaskBrowseSummaryModel).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ).values(effort=None))
        null_effort = (await session.execute(select(TaskBrowseSummaryModel.effort).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ))).scalar_one()
        for field in set(null_effort) - {"cost"}:
            assert null_effort[field] == sum(
                group[f"effort_{field}"] for group in row["groups"] or []
            )
        if not empty:
            assert null_effort["completed_trials"] == 1
            assert null_effort["steps_sum"] == 7
            assert null_effort["tokens_sum"] == 5_714_326_480
        # A current writer's complete snapshot must pass through unchanged.
        supplied = effort | {"output_tokens_sum": 123456}
        await session.execute(update(TaskBrowseSummaryModel).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ).values(effort=supplied))
        assert (await session.execute(select(TaskBrowseSummaryModel.effort).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ))).scalar_one() == supplied
        # Older writers can also create a summary for the first time.
        await session.execute(delete(TaskBrowseSummaryModel).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ))
        session.add(TaskBrowseSummaryModel(task_version_id=version, task_id=task, effort=old))
        await session.flush()
        assert (await session.execute(select(TaskBrowseSummaryModel.effort).where(
            TaskBrowseSummaryModel.task_version_id == version,
        ))).scalar_one() == effort
    async with engine.connect() as connection:
        await connection.run_sync(lambda conn: migrate(conn, downgrade=True))
        assert (await connection.execute(text(
            "SELECT to_regprocedure('fill_task_effort_token_breakdown()')"
        ))).scalar_one() is None
