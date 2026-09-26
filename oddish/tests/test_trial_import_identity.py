"""Import identity regression tests against PostgreSQL's actual column limits."""

from contextlib import asynccontextmanager
import os

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.ingest import trial_imports
from oddish.db import ExperimentModel, TaskModel, TrialModel, utcnow
from oddish.db import connection
from oddish.schemas import ImportedTrialSpec


@pytest_asyncio.fixture
async def import_session(monkeypatch):
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
                yield session

        monkeypatch.setattr(trial_imports, "get_session", get_session)
        try:
            async with get_session() as session:
                session.add_all(
                    [
                        TaskModel(
                            id="import-test",
                            name="import-test",
                            user="test",
                            task_path="test",
                            run_analysis=False,
                        ),
                        ExperimentModel(id="5fc7151e", name="import-first"),
                        ExperimentModel(id="exp-two", name="import-second"),
                    ]
                )
                await session.commit()
            yield get_session
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "external_id",
    [
        "short",
        "x" * 49,  # Exactly 64 characters after the prefix and experiment suffix.
        "x" * 50,
        "solidity-change-induced-code-fix-netpublic-eb4e457e-6",
        "日本語😀" * 100,
    ],
)
async def test_import_and_duplicate_replay(import_session, external_id):
    spec = ImportedTrialSpec(agent="codex", external_trial_id=external_id)
    kwargs = dict(
        task_id="import-test",
        experiment_id_or_name="5fc7151e",
        trial_spec=spec,
        upload_artifacts=False,
    )
    imported = await trial_imports.initialize_trial_import(**kwargs)
    async with import_session() as session:
        trial = await session.get(TrialModel, imported.trial_id)
        key = trial.idempotency_key
        assert len(key) <= 64
        original = f"import:{external_id}:5fc7151e"
        if len(original) <= 64:
            assert key == original

    # Both live and soft-deleted rows must retain their duplicate identity.
    for deleted in (False, True):
        if deleted:
            async with import_session() as session:
                trial = await session.get(TrialModel, imported.trial_id)
                trial.deleted_at = utcnow()
                await session.commit()
        with pytest.raises(HTTPException) as raised:
            await trial_imports.initialize_trial_import(**kwargs)
        assert raised.value.status_code == 409
        assert raised.value.detail["trial_id"] == imported.trial_id
        assert raised.value.detail["external_trial_id"] == external_id

    # The same source can still be imported into a different experiment.
    other = await trial_imports.initialize_trial_import(
        **{**kwargs, "experiment_id_or_name": "exp-two"}
    )
    async with import_session() as session:
        assert (await session.get(TrialModel, other.trial_id)).idempotency_key != key


@pytest.mark.asyncio
async def test_long_ids_with_shared_prefix_remain_distinct(import_session):
    ids = []
    for suffix in ("a", "b"):
        response = await trial_imports.initialize_trial_import(
            task_id="import-test",
            experiment_id_or_name="5fc7151e",
            trial_spec=ImportedTrialSpec(
                agent="codex", external_trial_id="x" * 100 + suffix
            ),
            upload_artifacts=False,
        )
        ids.append(response.trial_id)
    async with import_session() as session:
        keys = (
            await session.scalars(
                select(TrialModel.idempotency_key).where(TrialModel.id.in_(ids))
            )
        ).all()
    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_import_without_external_id_remains_additive(import_session):
    ids = []
    for _ in range(2):
        response = await trial_imports.initialize_trial_import(
            task_id="import-test",
            experiment_id_or_name="5fc7151e",
            trial_spec=ImportedTrialSpec(agent="codex"),
            upload_artifacts=False,
        )
        ids.append(response.trial_id)
    assert ids[0] != ids[1]
