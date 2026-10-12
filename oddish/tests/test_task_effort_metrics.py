"""Execute the task-detail effort aggregate against isolated PostgreSQL data."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from oddish.core.endpoints.task_open_aggregates import fold_task_open_groups
from oddish.core.endpoints.task_open_queries import AGGREGATE_SQL
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.db import TaskBrowseSummaryModel
from oddish.db.models import (
    Base,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
)
from oddish.schemas import TaskEffortSummary
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_effort_uses_exact_selected_version_completed_model_measurements():
    if not os.environ.get("ODDISH_TEST_DATABASE_URL"):
        pytest.skip("requires ODDISH_TEST_DATABASE_URL with CREATE DATABASE")
    url = make_url(os.environ["ODDISH_TEST_DATABASE_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "test_task_effort_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(
        url.set(database=name, drivername="postgresql+asyncpg")
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                TaskModel(id="task", name="task", user="test", task_path="/test")
            )
            session.add(ExperimentModel(id="experiment", name="experiment"))
            await session.flush()
            for v in (1, 2):
                session.add(
                    TaskVersionModel(
                        id=f"v{v}", task_id="task", version=v, task_path="/test"
                    )
                )
            await session.flush()
            await session.execute(
                update(TaskModel)
                .where(TaskModel.id == "task")
                .values(current_version_id="v2")
            )
            cases = [
                ("pass", {}),
                (
                    "fail",
                    {
                        "reward": 0,
                        "total_steps": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_tokens": 0,
                    },
                ),
                ("missing", {"total_steps": None, "input_tokens": None}),
                (
                    "timeout",
                    {
                        "status": "FAILED",
                        "error_message": "AgentTimeoutError",
                        "reward": 0,
                    },
                ),
                ("large", {"input_tokens": 5_714_326_425}),
                ("infra", {"error_message": "SandboxError"}),
                ("failed", {"status": "FAILED"}),
                ("active", {"status": "RUNNING"}),
                ("oracle", {"agent": "oracle"}),
                ("nop", {"agent": "agent-nop-custom"}),
                ("qa", {"kind": "qa"}),
                ("deleted", {"deleted_at": now}),
                ("copy", {"idempotency_key": "combine:copy"}),
                ("superseded", {"superseded_by_trial_id": "pass"}),
                ("old", {"task_version_id": "v1"}),
            ]
            for trial_id, overrides in cases:
                values = {
                    "id": trial_id,
                    "name": trial_id,
                    "task_id": "task",
                    "experiment_id": "experiment",
                    "task_version_id": "v2",
                    "agent": "codex",
                    "provider": "openai",
                    "queue_key": "test",
                    "status": "SUCCESS",
                    "reward": 1,
                    "total_steps": 10,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_tokens": 50,
                }
                values.update(overrides)
                session.add(TrialModel(**values))
                await session.flush()
        async with sessions() as session:
            row = (
                (
                    await session.execute(
                        AGGREGATE_SQL,
                        {
                            "task_id": "task",
                            "org_id": None,
                            "version_id": "v2",
                            "current_version_id": "v2",
                        },
                    )
                )
                .mappings()
                .one()
            )
            await refresh_task_browse_summaries(session, ["v2"])
            saved = await session.get(TaskBrowseSummaryModel, "v2")
            browse_effort = TaskEffortSummary.model_validate(saved.effort)
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).parents[1] / "alembic")
            )
            migration = (
                ScriptDirectory.from_config(config)
                .get_revision("task_effort_tokens_001")
                .module
            )
            await session.execute(text(migration._BACKFILL), {"ids": ["v2"]})
            await session.refresh(saved)
            assert TaskEffortSummary.model_validate(saved.effort) == browse_effort
        identity = {
            "selected_version_id": "v2",
            "selected_version": 2,
            "current_version_id": "v2",
            "selected_version_message": None,
            "selected_version_created_at": now,
            "must_fix_count": 0,
            "pre_trial_must_fix_count": 0,
        }
        _, selected, _ = fold_task_open_groups(row["groups"], identity, 0)
        assert selected.effort.model_dump(exclude={"cost"}) == {
            "completed_trials": 5,
            "steps_sum": 30,
            "steps_trial_count": 4,
            "tokens_sum": 5_714_326_685,
            "tokens_trial_count": 4,
            "uncached_input_tokens_sum": 5_714_326_475,
            "uncached_input_tokens_trial_count": 4,
            "cached_input_tokens_sum": 200,
            "cached_input_tokens_trial_count": 5,
            "output_tokens_sum": 80,
            "output_tokens_trial_count": 5,
        }
        assert browse_effort.model_dump(exclude={"cost"}) == selected.effort.model_dump(exclude={"cost"})
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()
