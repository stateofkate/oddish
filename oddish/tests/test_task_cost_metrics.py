"""Cost attribution, incomplete prices and old-writer snapshot compatibility."""

import os
from datetime import UTC, datetime
from uuid import uuid4
import asyncpg
import pytest
from test_exact_token_counts import db  # noqa: F401
from sqlalchemy import select, func, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from oddish.db.models import (
    Base,
    TaskModel,
    TaskVersionModel,
    ExperimentModel,
    TrialModel,
    AnalysisCostModel,
    TaskBrowseSummaryModel,
)
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.core.task_cost import CostMetric, cost_average_expression
from oddish.core.endpoints.task_open import get_task_open_core


@pytest.mark.asyncio
async def test_version_cost_attribution_and_legacy_writer():
    if not os.environ.get("ODDISH_TEST_DATABASE_URL"):
        pytest.skip("requires isolated PostgreSQL databases")
    url = make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_task_cost_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg")
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sessions.begin() as s:
            s.add_all(
                [
                    TaskModel(id="task", name="task", user="test", task_path="/test"),
                    ExperimentModel(id="exp", name="exp"),
                ]
            )
            await s.flush()
            s.add_all(
                [
                    TaskVersionModel(
                        id=f"v{v}", task_id="task", version=v, task_path="/test"
                    )
                    for v in (1, 2)
                ]
            )
            await s.flush()
            cases = [
                ("paid", {"cost_usd": 10}),
                ("free", {"cost_usd": 0}),
                ("baseline", {"agent": "oracle", "cost_usd": 999}),
                ("infra", {"error_message": "SandboxError", "cost_usd": 999}),
                ("old", {"task_version_id": "v1", "cost_usd": 30}),
                ("qa", {"kind": "qa", "cost_usd": 0.2}),
                ("eval", {"kind": "qa_eval", "cost_usd": 999}),
                (
                    "oldqa",
                    {"kind": "qa", "cost_usd": 999, "superseded_by_trial_id": "qa"},
                ),
                (
                    "deletedqa",
                    {"kind": "audit", "cost_usd": 999, "deleted_at": datetime.now(UTC)},
                ),
                (
                    "runningqa",
                    {"kind": "summarize", "status": "RUNNING", "cost_usd": None},
                ),
            ]
            for tid, extra in cases:
                s.add(
                    TrialModel(
                        id=tid,
                        name=tid,
                        task_id="task",
                        experiment_id="exp",
                        task_version_id=extra.pop("task_version_id", "v2"),
                        agent=extra.pop("agent", "codex"),
                        provider="openai",
                        queue_key="test",
                        status=extra.pop("status", "SUCCESS"),
                        reward=1,
                        **extra,
                    )
                )
            await s.flush()
            s.add(
                AnalysisCostModel(
                    id="legacy",
                    trial_id="paid",
                    job_kind="classifier",
                    cost_usd=0.4,
                    cost_source="native",
                )
            )
            await s.flush()
            s.add_all(
                [
                    AnalysisCostModel(
                        id="void-source",
                        trial_id="deletedqa",
                        job_kind="classifier",
                        cost_usd=999,
                        cost_source="native",
                    ),
                    AnalysisCostModel(
                        id="replaced-source",
                        trial_id="oldqa",
                        job_kind="classifier",
                        cost_usd=999,
                        cost_source="native",
                    ),
                    AnalysisCostModel(
                        id="pending",
                        trial_id="paid",
                        job_kind="classifier",
                        cost_usd=None,
                        cost_source="pending",
                    ),
                ]
            )
            await s.flush()
            cost = await s.scalar(select(func.task_cost_summary_v1("v2")))
            assert cost["qa_sum"] == pytest.approx(0.6)
            assert {k: v for k, v in cost.items() if k != "qa_sum"} == dict(
                cohort_version=2,
                completed_trials=2,
                trial_sum=10,
                trial_count=2,
                qa_jobs=2,
                qa_unpriced_jobs=0,
                qa_unattributed_jobs=0,
            )
            assert (await s.scalar(select(func.task_cost_summary_v1("v1"))))[
                "trial_sum"
            ] == 30
            await refresh_task_browse_summaries(s, ["v2"])
            snapshot = await s.get(TaskBrowseSummaryModel, "v2")
            assert snapshot.effort["cost"] == cost
            await refresh_task_browse_summaries(s, ["v1"])
            average = cost_average_expression(
                TaskBrowseSummaryModel.effort["cost"], CostMetric.TOTAL
            )
            rows = (
                (
                    await s.execute(
                        select(TaskBrowseSummaryModel.task_version_id)
                        .order_by(average.desc().nulls_last())
                        .limit(1)
                    )
                )
                .scalars()
                .all()
            )
            assert rows == ["v1"]  # ordering is applied before pagination
            opened = await get_task_open_core(
                s, task_id="task", org_id=None, version_id="v2"
            )
            assert opened.selected_version.effort.cost.qa_sum == pytest.approx(0.6)
            # An older writer replacing the entire JSON still gets current cost.
            await s.execute(
                update(TaskBrowseSummaryModel)
                .where(TaskBrowseSummaryModel.task_version_id == "v2")
                .values(effort={"completed_trials": 2})
            )
            await s.refresh(snapshot)
            assert snapshot.effort["cost"] == cost
            await s.execute(
                update(TrialModel).where(TrialModel.id == "qa").values(cost_usd=None)
            )
            assert (await s.scalar(select(func.task_cost_summary_v1("v2"))))[
                "qa_unpriced_jobs"
            ] == 1
            s.add(
                AnalysisCostModel(
                    id="untied",
                    task_id="task",
                    job_kind="audit",
                    cost_usd=0.5,
                    cost_source="native",
                )
            )
            await s.flush()
            assert (await s.scalar(select(func.task_cost_summary_v1("v2"))))[
                "qa_unattributed_jobs"
            ] == 1
            await s.refresh(snapshot)
            assert snapshot.effort["cost"]["qa_unattributed_jobs"] == 1

            await s.execute(
                update(AnalysisCostModel)
                .where(AnalysisCostModel.id == "untied")
                .values(created_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
            await s.refresh(snapshot)
            assert (
                snapshot.effort["cost"]["qa_unattributed_jobs"] == 0
            )  # predates the version
            await s.execute(
                update(TrialModel)
                .where(TrialModel.id == "free")
                .values(cost_usd=float("nan"))
            )
            assert (await s.scalar(select(func.task_cost_summary_v1("v2"))))[
                "trial_count"
            ] == 1
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_cost_migration_rebuilds_existing_snapshots(db):
    from pathlib import Path
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from test_task_run_coverage import seed

    engine, sessions = db
    async with sessions.begin() as session:
        task, version, _ = await seed(session, [{"cost_usd": 2.5}])
        session.add_all(
            [
                TaskVersionModel(
                    id=f"cost-batch-{i:03}",
                    task_id=task,
                    version=i + 2,
                    task_path="/test",
                )
                for i in range(202)
            ]
        )
        await session.flush()
        session.add_all(
            [
                TaskBrowseSummaryModel(
                    task_version_id=f"cost-batch-{i:03}", task_id=task, effort={}
                )
                for i in range(202)
            ]
        )
        await session.flush()
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
        ScriptDirectory.from_config(config).get_revision("task_cost_settled_002").module
    )

    def migrate(connection, downgrade=False):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            (migration.downgrade if downgrade else migration.upgrade)()

    async with engine.connect() as connection:
        await connection.run_sync(migrate, True)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE task_version_browse_summaries SET effort=effort-'cost'")
        )
    for _ in range(2):
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
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
@pytest.mark.parametrize("metric", ["total", "trial", "qa"])
async def test_cost_browse_sorting_precedes_pagination(db, metric):
    from oddish.core.endpoints.tasks_query import browse_tasks_core
    from test_task_run_coverage import seed

    _, sessions = db
    async with sessions() as session:
        ids = []
        for price in (0, 10, None, 3):
            task, _, _ = await seed(
                session,
                [
                    {"cost_usd": price},
                    {"kind": "qa", "cost_usd": price},
                    {"agent": "oracle", "cost_usd": 999},
                ],
                org="cost-org",
            )
            ids.append(task)
        for direction, expected in (
            ("asc", [ids[0], ids[3], ids[1], ids[2]]),
            ("desc", [ids[1], ids[3], ids[0], ids[2]]),
        ):
            seen = []
            for offset in range(4):
                page = await browse_tasks_core(
                    session,
                    org_id="cost-org",
                    sort=f"avg_{metric}_cost_{direction}",
                    limit=1,
                    offset=offset,
                )
                seen.append(page.items[0].id)
            assert seen == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["qa", "audit", "summarize"])
async def test_native_qa_settlement_refreshes_cost_snapshot(db, monkeypatch, kind):
    from contextlib import asynccontextmanager
    from oddish.workers.harbor.outcome import HarborOutcome
    from oddish.workers.queue import trial_handler
    from test_task_run_coverage import seed

    _, sessions = db
    async with sessions() as session:
        task, version, experiment = await seed(session, [{"cost_usd": 10}])
        row = TrialModel(
            id="settling-qa",
            org_id=(await session.get(TaskModel, task)).org_id,
            name="settling-qa",
            task_id=task,
            task_version_id=version,
            experiment_id=experiment,
            agent="terminus-2",
            provider="anthropic",
            model="anthropic/claude-opus-4-8",
            queue_key="test",
            harbor_config={"verifier": {"disable": True}},
            kind=kind,
            status=trial_handler.TrialStatus.RUNNING,
            attempts=1,
            max_attempts=1,
        )
        session.add(row)
        await session.flush()
        await refresh_task_browse_summaries(session, [version])
        snapshot = await session.get(TaskBrowseSummaryModel, version)
        assert snapshot.effort["cost"]["qa_sum"] == 0

        @asynccontextmanager
        async def existing_session(*args, **kwargs):
            yield session, row

        monkeypatch.setattr(trial_handler, "_trial_session", existing_session)
        outcome = HarborOutcome(
            reward=None,
            error=None,
            exit_code=0,
            duration_sec=1,
            job_result_path=None,
            job_dir=None,
            cost_usd=2,
            input_tokens=100,
            output_tokens=10,
        )
        terminal, _, _ = await trial_handler._store_trial_results(
            trial_id=row.id,
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
            trial_attempt=1,
        )
        assert terminal and row.status == trial_handler.TrialStatus.SUCCESS
        await session.refresh(snapshot)
        assert snapshot.effort["cost"]["qa_sum"] == row.cost_usd == 2
        assert snapshot.effort["cost"]["qa_jobs"] == 1
        assert snapshot.effort["cost"] == await session.scalar(
            select(func.task_cost_summary_v1(version))
        )
        # A later metering settlement uses the same production refresh path.
        from dataclasses import replace

        await trial_handler._store_trial_results(
            trial_id=row.id,
            outcome=replace(outcome, cost_usd=3),
            trial_s3_key=None,
            execution_error=None,
            trial_attempt=1,
        )
        await session.refresh(snapshot)
        assert snapshot.effort["cost"]["qa_sum"] == row.cost_usd == 3
