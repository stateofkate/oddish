"""Real PostgreSQL regression coverage for durable setup evidence."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
from fastapi import HTTPException
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oddish.core.progress_history import read_progress_history
from oddish.db.models import (
    Base,
    TaskModel,
    ExperimentModel,
    TrialModel,
    TrialEventModel,
    TrialProgressEventModel,
    TrialStatus,
)
from oddish.workers.harbor import live_tail_db


@pytest_asyncio.fixture
async def db(monkeypatch):
    if not os.environ.get("ODDISH_TEST_DATABASE_URL"):
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    url = make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_progress_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[1] / "alembic")
        )
        migration = (
            ScriptDirectory.from_config(config)
            .get_revision("trial_progress_001")
            .module
        )

        def migrate(conn):
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()

        def schema(conn):
            inspector = inspect(conn)
            table = "trial_progress_events"
            return {
                "columns": [
                    (c["name"], str(c["type"]), c["nullable"], c["default"])
                    for c in inspector.get_columns(table)
                ],
                "pk": inspector.get_pk_constraint(table),
                "fk": inspector.get_foreign_keys(table),
                "checks": inspector.get_check_constraints(table),
                "indexes": inspector.get_indexes(table),
            }

        async with engine.begin() as conn:
            baseline = await conn.run_sync(schema)
            # Fresh DB baseline already creates the latest ORM schema.
            await conn.run_sync(migrate)
            assert await conn.run_sync(schema) == baseline
            # Existing installations reach this migration without the new table.
            await conn.run_sync(TrialProgressEventModel.__table__.drop)
            await conn.run_sync(migrate)
            await conn.run_sync(migrate)
            assert await conn.run_sync(schema) == baseline
        async with sessions.begin() as s:
            s.add(
                TaskModel(
                    id="task", name="task", user="test", task_path="/test", org_id="org"
                )
            )
            s.add(ExperimentModel(id="experiment", name="experiment"))
        async with sessions.begin() as s:
            s.add(
                TrialModel(
                    id="trial",
                    name="trial",
                    task_id="task",
                    experiment_id="experiment",
                    agent="nop",
                    provider="default",
                    queue_key="test",
                    model="test",
                    attempts=1,
                )
            )

        @asynccontextmanager
        async def session():
            async with sessions.begin() as s:
                yield s

        monkeypatch.setattr(live_tail_db, "get_session", session)
        yield sessions
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def step(seq=1, status="started"):
    return {
        "seq": seq,
        "kind": "env_subphase",
        "payload": {
            "phase": "create",
            "status": status,
            "started_at": "2026-10-06T12:00:00Z",
            "duration_seconds": 1 if status != "started" else None,
        },
    }


async def ingest(events, attempt=1):
    return await live_tail_db.persist_tick_to_database(
        trial_id="trial",
        attempt=attempt,
        events=events,
        checkpoint_values=None,
        org_id=None,
        billed_user_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
async def test_history_survives_terminal_purge_replay_and_reload(db, status):
    await ingest([step(), step(2, "error" if status == "failed" else "success")])
    await ingest([step()])
    async with db.begin() as s:
        await s.execute(
            update(TrialModel)
            .where(TrialModel.id == "trial")
            .values(
                status=TrialStatus.SUCCESS
                if status == "success"
                else TrialStatus.FAILED,
                error_message="Cancelled by user" if status == "cancelled" else None,
                finished_at=datetime.now(timezone.utc),
            )
        )
    await live_tail_db.purge_events("trial", 1)
    async with db() as s:
        assert not (await s.scalars(select(TrialEventModel))).all()
        result = await read_progress_history(s, trial_id="trial", org_id="org")
        assert result.history_available
        assert [e.source_seq for e in result.events] == [1, 2]
        assert result.events[1].payload.duration_seconds == 1
    await ingest([step(2, "started")])
    async with db() as s:
        result = await read_progress_history(s, trial_id="trial", org_id="org")
        assert result.events[1].payload.status != "started"


@pytest.mark.asyncio
async def test_attempt_isolation_org_scope_legacy_and_physical_delete(db):
    async with db() as s:
        assert not (
            await read_progress_history(s, trial_id="trial", org_id="org")
        ).history_available
        with pytest.raises(HTTPException) as exc:
            await read_progress_history(s, trial_id="trial", org_id="other")
        assert exc.value.status_code == 404
    await ingest([step()])
    async with db.begin() as s:
        await s.execute(
            update(TrialModel).where(TrialModel.id == "trial").values(attempts=2)
        )
    await ingest([step(1, "error")], attempt=2)
    await live_tail_db.purge_events("trial", 2)
    async with db() as s:
        result = await read_progress_history(s, trial_id="trial", org_id="org")
        assert result.current_attempt == 2
        assert [(e.attempt, e.payload.status) for e in result.events] == [
            (2, "error"),
            (1, "started"),
        ]
    async with db.begin() as s:
        await s.execute(delete(TrialModel).where(TrialModel.id == "trial"))
    async with db() as s:
        assert not (await s.scalars(select(TrialProgressEventModel))).all()


@pytest.mark.asyncio
async def test_bounds_and_allowlist_do_not_retain_transcript(db):
    await ingest(
        [
            {"seq": 1, "kind": "message", "payload": {"secret": "not retained"}},
            {"seq": 2, "kind": "env_info", "payload": {"provider": "modal"}},
        ]
    )
    bad = step(3)
    bad["payload"]["phase"] = "x" * 129
    await ingest([bad])
    async with db() as s:
        result = await read_progress_history(s, trial_id="trial", org_id="org")
        assert len(result.events) == 1
        assert result.events[0].payload.external_id is None
        assert len((await s.scalars(select(TrialEventModel))).all()) == 3
    await ingest([step(5000)])
    async with db() as s:
        assert (
            await read_progress_history(s, trial_id="trial", org_id="org")
        ).history_truncated


@pytest.mark.asyncio
async def test_atomicity_and_concurrent_purge(db):
    import asyncio
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await ingest([step(), {"seq": 2, "kind": None, "payload": {}}])
    async with db() as s:
        assert not (await s.scalars(select(TrialProgressEventModel))).all()
    await asyncio.gather(ingest([step()]), live_tail_db.purge_events("trial", 1))
    async with db() as s:
        assert (
            len((await read_progress_history(s, trial_id="trial", org_id="org")).events)
            == 1
        )


def test_progress_contract_discriminates_payloads():
    from oddish.progress import ProgressEvent

    schema = ProgressEvent.model_json_schema()
    assert schema["properties"]["payload"]["discriminator"]["propertyName"] == "kind"


@pytest.mark.asyncio
async def test_writer_rejects_future_and_stale_attempt_attribution(db):
    await ingest([step()], attempt=2)
    async with db() as s:
        assert not (
            await read_progress_history(s, trial_id="trial", org_id="org")
        ).history_available
    await ingest([step()], attempt=1)
    async with db.begin() as s:
        await s.execute(
            update(TrialModel).where(TrialModel.id == "trial").values(attempts=2)
        )
    await ingest([step(2, "error")], attempt=1)
    await ingest([step(1, "success")], attempt=2)
    async with db() as s:
        result = await read_progress_history(s, trial_id="trial", org_id="org")
        assert [(e.attempt, e.source_seq) for e in result.events] == [(2, 1), (1, 1)]


@pytest.mark.asyncio
async def test_v2_parallel_operations_retry_and_cancel_survive_purge(db):
    events = []
    for seq, (operation, status) in enumerate([
        ("a", "started"), ("b", "started"), ("a", "success"), ("b", "cancelled")
    ], 1):
        event = step(seq, status)
        event["payload"].update(schema_version=2, operation_id=operation,
                                parent_operation_id="root", category="build",
                                provider="docker", strategy="compose", operation_attempt=2)
        events.append(event)
    await ingest(events)
    await live_tail_db.purge_events("trial", 1)
    async with db() as s:
        history = await read_progress_history(s, trial_id="trial", org_id="org")
        assert [(e.payload.operation_id, e.payload.status) for e in history.events] == [
            ("a", "started"), ("b", "started"), ("a", "success"), ("b", "cancelled")
        ]
        assert all(e.schema_version == 2 for e in history.events)
        assert all(e.payload.operation_attempt == 2 for e in history.events)
        assert all(e.payload.provider == "docker" for e in history.events)


@pytest.mark.parametrize("patch", [
    {"schema_version": 2},
    {"operation_id": "a", "parent_operation_id": "a"},
    {"category": "invented"},
    {"operation_attempt": 0},
    {"resource_layer": "outer_worker"},
])
def test_progress_contract_rejects_invalid_operation_identity(patch):
    from pydantic import ValidationError
    from oddish.progress import EnvSubphasePayload

    with pytest.raises(ValidationError):
        EnvSubphasePayload.model_validate({**step()["payload"], **patch})


def completed_activity(**kwargs):
    return {
        "schema_version": 2,
        "operation_id": "build",
        "parent_operation_id": "root",
        "phase": "environment.build",
        "provider": "docker",
        "status": "success",
        "started_at": "2026-10-06T12:00:00Z",
        "finished_at": "2026-10-06T12:00:02Z",
        "duration_seconds": 2,
        **kwargs,
    }


@pytest.mark.parametrize(
    "activity",
    [
        completed_activity(status="started"),
        completed_activity(finished_at=None),
        completed_activity(duration_seconds=float("inf")),
        completed_activity(activity_kind="invented"),
        completed_activity(operation_id=""),
        completed_activity(completed_activities=[]),
    ],
)
def test_completed_measurements_require_bounded_terminal_facts(activity):
    from oddish.progress import EnvSubphasePayload
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EnvSubphasePayload.model_validate(
            {
                **step()["payload"],
                "completed_activities": [activity],
            }
        )


def test_activity_batch_bound_and_legacy_presence():
    from oddish.progress import EnvSubphasePayload
    from pydantic import ValidationError

    assert (
        EnvSubphasePayload.model_validate(step()["payload"]).completed_activities
        is None
    )
    assert (
        EnvSubphasePayload.model_validate(
            {
                **step()["payload"],
                "completed_activities": [],
            }
        ).completed_activities
        == []
    )
    with pytest.raises(ValidationError):
        EnvSubphasePayload.model_validate(
            {
                **step()["payload"],
                "completed_activities": [completed_activity()] * 129,
            }
        )


@pytest.mark.asyncio
async def test_corrected_completed_activity_snapshots_retained_without_extra_rows(db):
    first = step(1, "success")
    first["payload"].update(schema_version=2, operation_id="build")
    snapshot = step(2, "success")
    snapshot["payload"].update(
        schema_version=2,
        operation_id="root",
        category="setup",
        completed_activities=[
            completed_activity(),
            completed_activity(
                operation_id="delivery",
                parent_operation_id="build",
                phase="progress.delivery.finished",
                activity_kind="supporting",
                duration_seconds=0.2,
            ),
        ],
    )
    await ingest([first, snapshot])
    await live_tail_db.purge_events("trial", 1)
    async with db() as s:
        history = await read_progress_history(s, trial_id="trial", org_id="org")
        assert len(history.events) == 2
        assert history.events[0].payload.duration_seconds == 1
        activities = history.events[1].payload.completed_activities
        assert activities[0].operation_id == history.events[0].payload.operation_id
        assert activities[0].duration_seconds == 2
        assert activities[1].parent_operation_id == "build"
        assert activities[1].activity_kind == "supporting"
