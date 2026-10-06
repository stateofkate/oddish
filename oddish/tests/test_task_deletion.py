from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints
from oddish.db import ExperimentModel, TaskModel


class _FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)

    def one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) != 1:
            raise AssertionError("Expected exactly one row")
        return self._rows[0]


class _FakeDeleteTaskSession:
    """Lightweight async-session fake that records every executed statement.

    Returns row payloads in a fixed order: first the task lookup, then the
    trial-id projection. Everything else returns an empty result set. The
    soft-delete path runs three writes after the lookups (worker_jobs
    cancel on trials, ``UPDATE trials``, ``UPDATE tasks``), and we only
    care about the *kind* of statement emitted, so this fake is enough.
    """

    def __init__(
        self,
        task_rows: list[tuple[str, str | None, str]],
        trial_rows: list[tuple[str]],
    ):
        self.task_rows = task_rows
        self.trial_rows = trial_rows
        self.statements = []
        self.delete_called = False

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _FakeRowsResult(self.task_rows)
        if len(self.statements) == 2:
            return _FakeRowsResult(self.trial_rows)
        return _FakeRowsResult([])

    async def delete(self, _obj):
        self.delete_called = True
        raise AssertionError("delete_task_core should not call session.delete()")


@pytest.mark.asyncio
async def test_delete_task_core_soft_deletes_task_and_trials():
    """Unscoped delete tombstones both trials and the task in-place.

    Soft delete keeps S3 artifacts (the response carries an empty
    ``s3_prefixes`` list so the API-layer best-effort S3 cleanup is a
    no-op) and emits UPDATE statements -- not DELETE -- against
    ``trials`` and ``tasks``. The session-level filter then hides those
    rows from normal reads.
    """
    task_rows = [("task-123", "tasks/task-123/", "s3://tasks/task-123/")]
    trial_rows = [("task-123-0",), ("task-123-1",)]
    session = _FakeDeleteTaskSession(task_rows, trial_rows)

    result = await endpoints.delete_task_core(session, task_id="task-123")

    assert result == {
        "s3_prefixes": [],
        "deleted": {"task_id": "task-123"},
        "modal_function_call_ids": [],
        "worker_targets": [],
    }
    assert session.delete_called is False

    # statements[0] task lookup, statements[1] trial lookup.
    write_statements = session.statements[2:]
    # Soft delete emits in order:
    #   1. UPDATE worker_jobs ... WHERE subject_table='trials'  (text())
    #   2. UPDATE worker_jobs ... WHERE subject_table='tasks'   (text())
    #   3. UPDATE trials SET deleted_at = ...                   (ORM update)
    #   4. UPDATE tasks  SET deleted_at = ...                   (ORM update)
    assert len(write_statements) == 4
    domain_writes = write_statements[2:]
    table_names = [
        stmt.table.name  # ORM ``update(Model)`` exposes ``.table``
        for stmt in domain_writes
    ]
    assert table_names == ["trials", "tasks"]
    # synchronize_session=False keeps the bulk UPDATEs cheap; the
    # session-level soft-delete filter handles future reads.
    assert all(
        stmt.get_execution_options().get("synchronize_session") is False
        for stmt in domain_writes
    )


def test_task_relationships_use_passive_deletes():
    """Foreign-key cascades remain intact for the rare hard-delete path.

    Soft-delete is the normal path, but admin / restore tooling can still
    issue a hard DELETE (``include_deleted=True`` plus a manual delete),
    and we want those passive-delete contracts to hold so the database
    cleans up children without round-tripping through the ORM.
    """
    assert ExperimentModel.tasks.property.passive_deletes is True
    assert TaskModel.trials.property.passive_deletes is True
    assert TaskModel.versions.property.passive_deletes is True


def test_soft_delete_models_registered():
    """The session-level soft-delete filter knows about the core domain rows."""
    from oddish.db.soft_delete import get_soft_delete_models
    from oddish.db import TrialModel

    registered = set(get_soft_delete_models())
    assert {ExperimentModel, TaskModel, TrialModel}.issubset(registered)


@pytest.mark.asyncio
async def test_batch_delete_then_restore(session):
    """Batch delete hides the task and its trials; batch restore brings back
    the task and the trials that delete hid, but not a trial deleted on its
    own before. Unknown IDs are reported, not fatal, and dry runs change
    nothing."""
    from sqlalchemy import select

    from oddish.db.models import TrialModel, generate_id

    task = TaskModel(
        name=f"restore-{generate_id()}", org_id="org1", user="t", task_path="s3://t"
    )
    experiment = ExperimentModel(name=f"restore-{generate_id()}", org_id="org1")
    session.add_all([task, experiment])
    await session.flush()
    trials = [
        TrialModel(
            id=generate_id(),
            name="t",
            task_id=task.id,
            experiment_id=experiment.id,
            org_id="org1",
            agent="nop",
            provider="none",
            queue_key="nop",
            model="nop",
        )
        for _ in range(2)
    ]
    session.add_all(trials)
    await session.flush()
    await endpoints.delete_trial_core(session, trial_id=trials[0].id, org_id="org1")
    ids = [task.id, "no-such-task"]

    async def live_trials():
        session.expunge_all()
        query = select(TrialModel.id).where(TrialModel.task_id == task.id)
        return (await session.execute(query)).scalars().all()

    dry = await endpoints.delete_tasks_core(
        session, task_ids=ids, org_id="org1", dry_run=True
    )
    assert (dry["found"], dry["missing"]) == ([task.id], ["no-such-task"])
    assert await live_trials() == [trials[1].id]

    deleted = await endpoints.delete_tasks_core(session, task_ids=ids, org_id="org1")
    assert deleted["found"] == [task.id]
    assert await live_trials() == []
    assert await session.get(TaskModel, task.id) is None

    dry = await endpoints.restore_tasks_core(
        session, task_ids=ids, org_id="org1", dry_run=True
    )
    assert (dry["found"], dry["trials_restored"]) == ([task.id], 0)
    assert await session.get(TaskModel, task.id) is None

    restored = await endpoints.restore_tasks_core(session, task_ids=ids, org_id="org1")
    assert (restored["found"], restored["missing"]) == ([task.id], ["no-such-task"])
    assert restored["trials_restored"] == 1
    assert await live_trials() == [trials[1].id]
    assert await session.get(TaskModel, task.id) is not None
    # Restoring a live task changes nothing.
    again = await endpoints.restore_tasks_core(session, task_ids=ids, org_id="org1")
    assert (again["found"], again["trials_restored"]) == ([], 0)


@pytest.mark.asyncio
async def test_restore_skips_task_whose_name_was_reused(session):
    """A deleted task's name can be reused; restoring it would break the
    one-live-task-per-(org, name) index, so it is reported and left deleted
    while the rest of the batch restores."""
    from oddish.db.models import generate_id

    name = f"reused-{generate_id()}"
    old = TaskModel(name=name, org_id="org1", user="t", task_path="s3://t")
    other = TaskModel(
        name=f"other-{generate_id()}", org_id="org1", user="t", task_path="s3://t"
    )
    session.add_all([old, other])
    await session.flush()
    await endpoints.delete_tasks_core(
        session, task_ids=[old.id, other.id], org_id="org1"
    )
    session.add(TaskModel(name=name, org_id="org1", user="t", task_path="s3://t"))
    await session.flush()

    result = await endpoints.restore_tasks_core(
        session, task_ids=[old.id, other.id], org_id="org1"
    )

    assert (result["found"], result["name_taken"]) == ([other.id], [old.id])
    session.expunge_all()
    assert await session.get(TaskModel, old.id) is None
    assert await session.get(TaskModel, other.id) is not None
