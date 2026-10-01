"""Exercise migration against both deployed and metadata-bootstrap schemas."""

import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrapped", [False, True])
async def test_report_feedback_migration_preserves_existing_votes(bootstrapped):
    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db.connection import engine

    path = Path(__file__).parents[1] / "alembic/versions/feedback_reports_001.py"
    spec = importlib.util.spec_from_file_location("feedback_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            CREATE TEMP TABLE feedback (
                id text PRIMARY KEY, target text NOT NULL,
                CONSTRAINT ck_feedback_target CHECK (target IN ('qa_verdict', 'qa_action_item'))
            ) ON COMMIT DROP
        """)
        )
        await connection.execute(
            text("INSERT INTO feedback VALUES ('old', 'qa_verdict')")
        )
        if bootstrapped:
            await connection.execute(
                text("ALTER TABLE feedback ADD COLUMN report_trial_id varchar(160)")
            )

        def upgrade(sync_connection):
            migration.op = Operations(MigrationContext.configure(sync_connection))
            migration.upgrade()

        await connection.run_sync(upgrade)
        assert (
            await connection.execute(
                text("SELECT target FROM feedback WHERE id = 'old'")
            )
        ).scalar() == "qa_verdict"
        for target in ("qa_trajectory", "qa_task_verdict", "qa_pre_trial"):
            await connection.execute(
                text(
                    "INSERT INTO feedback (id, target, report_trial_id) VALUES (:target, :target, 'run-1')"
                ),
                {"target": target},
            )

        def downgrade(sync_connection):
            migration.op = Operations(MigrationContext.configure(sync_connection))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await connection.run_sync(downgrade)
        await connection.execute(text("DELETE FROM feedback WHERE id != 'old'"))
        await connection.run_sync(downgrade)
        assert (
            await connection.execute(text("SELECT target FROM feedback"))
        ).scalar() == "qa_verdict"
