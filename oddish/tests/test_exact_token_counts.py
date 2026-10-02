"""Real PostgreSQL coverage for rolling old/new token writers and repairs."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oddish.core import backfill_token_counts as repair
from oddish.core.cost_basis import settled_cost_columns
from oddish.core.task_browse_metrics import browse_cost_group_columns
from oddish.db.models import (
    INT32_MAX,
    AnalysisCostModel,
    Base,
    ExperimentModel,
    TaskModel,
    TrialModel,
    VerifierCostModel,
)
from oddish.workers.harbor import live_tail_db

LARGE = 5_714_326_425


async def migrate(engine):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "alembic")
    )
    migration = (
        ScriptDirectory.from_config(config).get_revision("tokens_exact_001").module
    )

    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()

    async with engine.connect() as connection:
        await connection.run_sync(run)


@pytest_asyncio.fixture
async def db():
    url = (
        make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
        if os.environ.get("ODDISH_TEST_DATABASE_URL")
        else None
    )
    if url is None:
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_exact_tokens_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                TaskModel(id="task", name="task", user="test", task_path="/test")
            )
            session.add(ExperimentModel(id="experiment", name="experiment"))
        async with sessions.begin() as session:
            session.add(
                TrialModel(
                    id="trial",
                    name="trial",
                    task_id="task",
                    experiment_id="experiment",
                    agent="nop",
                    provider="default",
                    queue_key="test",
                    model="test-model",
                )
            )
        # Exercise an existing populated int32 database, not just create_all.
        async with engine.begin() as connection:
            for table, columns in repair.TOKEN_COLUMNS.items():
                for column in columns:
                    await connection.execute(
                        text(f"ALTER TABLE {table} DROP COLUMN {column}_exact")
                    )
            await connection.execute(
                text("UPDATE trials SET input_tokens = 123 WHERE id = 'trial'")
            )
        await migrate(engine)
        yield engine, sessions
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_migration_is_additive_restartable_and_preserves_rows(db):
    engine, sessions = db
    async with engine.connect() as connection:
        before = await connection.scalar(
            text("SELECT relfilenode FROM pg_class WHERE oid = 'trials'::regclass")
        )
    await migrate(engine)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT relfilenode FROM pg_class WHERE oid = 'trials'::regclass")
            )
            == before
        )
    async with sessions() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens == 123
        assert trial.input_tokens_exact is None


@pytest.mark.asyncio
async def test_live_checkpoint_final_save_and_all_twelve_exact_columns(db, monkeypatch):
    engine, sessions = db

    @asynccontextmanager
    async def get_session():
        async with sessions.begin() as session:
            yield session

    monkeypatch.setattr(live_tail_db, "get_session", get_session)
    persisted = await live_tail_db.persist_tick_to_database(
        trial_id="trial",
        attempt=1,
        events=[{"seq": 1, "kind": "assistant", "payload": {"text": "saved"}}],
        checkpoint_values={column: LARGE for column in repair.TOKEN_COLUMNS["trials"]},
        org_id=None,
        billed_user_id=None,
    )
    assert persisted.checkpoint_written and persisted.flushed == 1
    async with sessions.begin() as session:
        trial = await session.get(TrialModel, "trial")
        trial.reward = 1
        trial.trial_s3_key = "trials/trial/attempt-1/"
        trial.cost_usd = 12.5
        trial.finished_at = datetime.now(timezone.utc)
        session.add(
            AnalysisCostModel(
                id="analysis",
                job_kind="audit",
                cost_source="native",
                **{column: LARGE for column in repair.TOKEN_COLUMNS["analysis_costs"]},
            )
        )
        session.add(
            VerifierCostModel(
                id="verifier",
                trial_id="trial",
                attempt=1,
                component="cua_judge",
                route="anthropic",
                cost_source="native",
                **{column: LARGE for column in repair.TOKEN_COLUMNS["verifier_costs"]},
            )
        )
    async with sessions() as session:
        for model in (TrialModel, AnalysisCostModel, VerifierCostModel):
            row = (await session.scalars(select(model))).one()
            for column in repair.TOKEN_COLUMNS[model.__tablename__]:
                assert getattr(row, column) == LARGE
                assert getattr(row, column + "_legacy") == INT32_MAX
        trial = await session.get(TrialModel, "trial")
        assert (trial.reward, trial.cost_usd, trial.trial_s3_key) == (
            1,
            12.5,
            "trials/trial/attempt-1/",
        )
        result = (await session.execute(select(TrialModel.input_tokens))).one()
        assert result.input_tokens == LARGE


@pytest.mark.asyncio
async def test_old_writer_invalidates_even_an_unchanged_cap_and_new_writer_recovers(db):
    engine, sessions = db
    async with sessions.begin() as session:
        await session.execute(
            update(TrialModel).values(input_tokens=LARGE, output_tokens=42)
        )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE trials SET input_tokens = :cap"), {"cap": INT32_MAX}
        )
    async with sessions.begin() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens_exact is None
        assert trial.input_tokens == INT32_MAX
        assert trial.output_tokens_exact == 42
        trial.input_tokens = LARGE + 1
    async with sessions.begin() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens == LARGE + 1
        trial.input_tokens = None
        assert trial.input_tokens is None
    async with sessions() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens is None and trial.input_tokens_legacy is None


@pytest.mark.asyncio
async def test_legacy_null_zero_and_explicit_clear(db):
    engine, sessions = db
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE trials SET input_tokens=42, output_tokens=0")
        )
    async with sessions.begin() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.output_tokens == 0 and trial.cache_tokens is None
        trial.input_tokens = None
    async with sessions() as session:
        assert (await session.get(TrialModel, "trial")).input_tokens is None


@pytest.mark.asyncio
async def test_cost_arithmetic_widens_before_adding_and_preserves_recorded_cost(db):
    engine, sessions = db
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE trials SET input_tokens=:cap, cache_tokens=:cap, cache_write_tokens=100"
            ),
            {"cap": INT32_MAX},
        )
    async with sessions.begin() as session:
        for columns in (settled_cost_columns(), browse_cost_group_columns()):
            row = (await session.execute(select(*columns))).one()
            assert row.est_input == INT32_MAX + 100
        await session.execute(
            update(TrialModel).values(cost_usd=17.25, input_tokens=LARGE)
        )
        row = (await session.execute(select(*settled_cost_columns()))).one()
        assert row.native_cost == 17.25 and row.est_input == 0


@pytest.mark.asyncio
async def test_backfill_skips_locked_rows_and_never_overwrites_new_usage(db):
    engine, sessions = db
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE trials SET finished_at=now(), cache_tokens=:cap, cost_usd=17.25"
            ),
            {"cap": INT32_MAX},
        )
    async with sessions.begin() as writer:
        await writer.execute(
            update(TrialModel)
            .where(TrialModel.id == "trial")
            .values(input_tokens=LARGE)
        )
        async with sessions.begin() as backfill:
            assert (await repair.backfill_batch(backfill, table="trials"))[
                "scanned"
            ] == 0
    async with sessions.begin() as session:
        assert (await repair.backfill_batch(session, table="trials"))[
            "fields_updated"
        ] == 0
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens == LARGE and trial.cache_tokens_exact is None
        assert trial.cost_usd == 17.25
    # A writer arriving after the backfill's row lock must win after it commits.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE trials SET input_tokens=123"))
    async with sessions.begin() as session:
        assert (await repair.backfill_batch(session, table="trials"))[
            "fields_updated"
        ] == 1

        async def live_write():
            async with sessions.begin() as writer:
                await writer.execute(
                    update(TrialModel)
                    .where(TrialModel.id == "trial")
                    .values(input_tokens=LARGE)
                )

        write = asyncio.create_task(live_write())
        await asyncio.sleep(0.05)
        assert not write.done()
    await asyncio.wait_for(write, 5)
    async with sessions() as session:
        assert (await session.get(TrialModel, "trial")).input_tokens == LARGE


@pytest.mark.asyncio
async def test_task_detail_raw_sql_and_partial_load_read_exact_usage(db):
    from sqlalchemy.orm import load_only
    from oddish.core.endpoints.task_open_queries import AGGREGATE_SQL, PREVIEW_SQL
    from oddish.db.models import TaskVersionModel, TrialStatus

    engine, sessions = db
    playback = {"manifest_path": "verifier/artifacts/playback.json"}
    async with sessions.begin() as session:
        session.add(
            TaskVersionModel(id="version", task_id="task", version=1, task_path="/test")
        )
        await session.flush()
        await session.execute(
            update(TrialModel).values(
                task_version_id="version",
                input_tokens=LARGE,
                output_tokens=10,
                result={"_playback": playback},
            )
        )
    async with sessions() as session:
        params = {
            "task_id": "task",
            "version_id": "version",
            "current_version_id": "version",
            "org_id": None,
        }
        preview = (await session.execute(PREVIEW_SQL, params)).one()
        assert preview.input_tokens == LARGE
        assert preview.playback == playback
        aggregate = (await session.execute(AGGREGATE_SQL, params)).one()
        assert aggregate.groups[0]["token_count"] == LARGE + 10
        trial = (
            await session.scalars(
                select(TrialModel).options(
                    load_only(
                        TrialModel.input_tokens_exact,
                        TrialModel.input_tokens_legacy,
                        raiseload=True,
                    )
                )
            )
        ).one()
        assert trial.input_tokens == LARGE

    async with sessions.begin() as session:
        session.add(
            TrialModel(
                id="qa",
                name="qa",
                task_id="task",
                task_version_id="version",
                experiment_id="experiment",
                agent="tbh",
                provider="meta",
                queue_key="test",
                kind="qa",
                status=TrialStatus.RUNNING,
                input_tokens=LARGE,
                result={"_playback": playback},
            )
        )
    async with sessions() as session:
        aggregate = (await session.execute(AGGREGATE_SQL, params)).one()
        assert aggregate.active_qa_trial["input_tokens"] == LARGE
        assert aggregate.active_qa_trial["playback"] == playback


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_write", [False, True])
async def test_artifact_recovery_preserves_cost_and_rejects_concurrent_writes(
    db, monkeypatch, intervening_write
):
    import json
    from oddish.core.trial_artifacts import TrialArtifactLayout, TrialArtifactMode

    engine, sessions = db
    async with engine.begin() as connection:
        await connection.execute(
            text("""UPDATE trials SET input_tokens=:cap,
            finished_at=now(), attempts=1, cost_usd=17.25,
            trial_s3_key='trials/trial/attempt-1/'"""),
            {"cap": INT32_MAX},
        )
    raw = {
        "task_name": "task",
        "trial_name": "child",
        "trial_uri": "file:///trial",
        "task_id": {"path": "/task"},
        "task_checksum": "hash",
        "config": {"task": {"path": "/task"}},
        "agent_info": {"name": "nop", "version": "1"},
        "agent_result": {
            "n_input_tokens": LARGE,
            "n_output_tokens": 42,
            "cost_usd": 999,
        },
    }

    class Storage:
        async def download_text(self, key):
            assert key == "trials/trial/attempt-1/child/result.json"
            if intervening_write:
                async with sessions.begin() as writer:
                    await writer.execute(
                        update(TrialModel)
                        .where(TrialModel.id == "trial")
                        .values(input_tokens=LARGE + 100)
                    )
            return json.dumps(raw)

    async def layout(trial, storage):
        return TrialArtifactLayout(
            TrialArtifactMode.EXACT,
            "trials/trial/attempt-1/",
            "trials/trial/attempt-1/child/",
        )

    async def trajectory(trial):
        return {"final_metrics": {"cache_write_tokens": 100}}

    monkeypatch.setattr(repair, "get_storage_client", Storage)
    monkeypatch.setattr(repair, "resolve_trial_artifact_layout", layout)
    monkeypatch.setattr(repair, "read_trial_trajectory", trajectory)
    async with sessions.begin() as session:
        if intervening_write:
            with pytest.raises(ValueError, match="changed during recovery"):
                await repair.recover_trial(session, trial_id="trial")
        else:
            recovered = await repair.recover_trial(session, trial_id="trial")
            assert recovered["recovered"]["input_tokens_exact"] == LARGE
    async with sessions() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens == LARGE + (100 if intervening_write else 0)
        assert trial.cost_usd == 17.25
        assert trial.cache_tokens_exact is None  # Missing evidence stays unknown.


@pytest.mark.asyncio
async def test_verifier_upsert_uses_exact_columns_for_insert_and_conflict(db):
    from dataclasses import replace
    from oddish.costs.verifier_cost import VerifierCostDraft, upsert_verifier_cost_rows

    engine, sessions = db
    draft = VerifierCostDraft(
        component="cua_judge",
        model="test",
        route="anthropic",
        llm_key_hash=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        cost_usd=None,
        cost_source="native",
        unpriced_reason="no_cua_artifacts",
    )
    async with sessions.begin() as session:
        for value in (
            draft,
            replace(draft, input_tokens=LARGE, cost_usd=12.5, unpriced_reason=None),
        ):
            assert (
                await upsert_verifier_cost_rows(
                    session,
                    drafts=[value],
                    trial_id="trial",
                    attempt=1,
                    experiment_id="experiment",
                    org_id=None,
                    task_id="task",
                    task_version_id=None,
                )
                == 1
            )
    async with sessions() as session:
        row = (await session.scalars(select(VerifierCostModel))).one()
        assert row.input_tokens == LARGE and row.input_tokens_legacy == INT32_MAX
        assert row.cost_usd == 12.5


@pytest.mark.asyncio
async def test_untrusted_artifacts_cannot_lower_the_stored_count(db, monkeypatch):
    import json
    from oddish.core.trial_artifacts import TrialArtifactLayout, TrialArtifactMode

    engine, sessions = db
    async with engine.begin() as connection:
        await connection.execute(
            text("""UPDATE trials SET input_tokens=:cap,
            finished_at=now(), attempts=1, trial_s3_key='trials/trial/attempt-1/'"""),
            {"cap": INT32_MAX},
        )

    class Storage:
        async def download_text(self, key):
            return json.dumps(
                {
                    "task_name": "task",
                    "trial_name": "child",
                    "trial_uri": "file:///trial",
                    "task_id": {"path": "/task"},
                    "task_checksum": "hash",
                    "config": {"task": {"path": "/task"}},
                    "agent_info": {"name": "nop", "version": "1"},
                    "agent_result": {"n_input_tokens": 12},
                }
            )

    async def layout(trial, storage):
        return TrialArtifactLayout(
            TrialArtifactMode.EXACT,
            "trials/trial/attempt-1/",
            "trials/trial/attempt-1/child/",
        )

    async def trajectory(trial):
        return None

    monkeypatch.setattr(repair, "get_storage_client", Storage)
    monkeypatch.setattr(repair, "resolve_trial_artifact_layout", layout)
    monkeypatch.setattr(repair, "read_trial_trajectory", trajectory)
    async with sessions.begin() as session:
        with pytest.raises(ValueError, match="below the stored lower bound"):
            await repair.recover_trial(session, trial_id="trial")
    async with sessions() as session:
        trial = await session.get(TrialModel, "trial")
        assert trial.input_tokens == INT32_MAX and trial.input_tokens_exact is None
