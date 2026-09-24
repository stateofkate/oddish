"""Run coverage, exact scores and selection agreement against PostgreSQL."""

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, text, update

from oddish.core.endpoints.tasks_query import browse_tasks_core
from oddish.core.endpoints.task_open import get_task_open_core
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TaskBrowseSummaryModel,
    TrialModel,
    TrialStatus,
)
from oddish.schemas import TaskRunSummary


async def seed(session, specs=(), *, org=None):
    task_id = uuid.uuid4().hex
    org = org or task_id
    version_id = f"{task_id}-v1"
    session.add(ExperimentModel(id=task_id, name=task_id, org_id=org))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org,
            user="tester",
            task_path="s3://test/run-coverage",
        )
    )
    await session.flush()
    session.add(
        TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=1,
            task_path="s3://test/run-coverage",
        )
    )
    await session.flush()
    await session.execute(
        update(TaskModel)
        .where(TaskModel.id == task_id)
        .values(current_version_id=version_id)
    )
    for i, spec in enumerate(specs):
        values = dict(
            agent="codex",
            model="test-model",
            provider="openai",
            queue_key="test",
            status=TrialStatus.SUCCESS,
            reward=1.0,
        )
        values.update(spec)
        session.add(
            TrialModel(
                id=f"{task_id}-{i}",
                name=f"run-{i}",
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=task_id,
                org_id=org,
                **values,
            )
        )
    await refresh_task_browse_summaries(session, [version_id])
    return task_id, version_id, org


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "specs,coverage,scored",
    [
        ([], "no_trials", 0),
        (
            [{"kind": "qa"}, {"idempotency_key": "combine:test"}],
            "no_trials",
            0,
        ),
        ([{"agent": "oracle"}], "validation_only", 0),
        ([{"agent": "agent-nop-test", "reward": 0}], "validation_only", 0),
        (
            [{"agent": "oracle-test"}, {"agent": "nop", "reward": 0}],
            "validation_only",
            0,
        ),
        ([{"status": TrialStatus.PENDING, "reward": None}], "model_trials", 0),
        (
            [{"status": TrialStatus.FAILED, "error_message": "boom", "reward": 1}],
            "model_trials",
            0,
        ),
        ([{"reward": None}], "model_trials", 0),
        ([{"status": TrialStatus.SKIPPED, "reward": 1}], "model_trials", 0),
        (
            [
                {
                    "reward": 0.5,
                    "status": TrialStatus.FAILED,
                    "error_message": "AgentTimeoutError",
                }
            ],
            "model_trials",
            1,
        ),
        ([{"origin": "imported", "reward": 0.25}], "model_trials", 1),
    ],
)
async def test_coverage_page_count_ids_and_open(session, specs, coverage, scored):
    task, version, org = await seed(session, specs)
    for choice in (
        "any",
        "no_trials",
        "validation_only",
        "model_trials",
        "scored_model_trials",
    ):
        matches = choice in ("any", coverage) or (
            choice == "scored_model_trials" and scored > 0
        )
        page = await browse_tasks_core(session, org_id=org, run_coverage=choice)
        count = await browse_tasks_core(
            session, org_id=org, run_coverage=choice, count_only=True
        )
        ids = await browse_tasks_core(
            session, org_id=org, run_coverage=choice, ids_only=True
        )
        assert [item.id for item in page.items] == ([task] if matches else [])
        assert count == int(matches)
        assert ids == ([task] if matches else [])
    page = await browse_tasks_core(session, org_id=org)
    opened = await get_task_open_core(session, task_id=task, org_id=org)
    assert page.items[0].run_summary == opened.selected_version.run_summary
    assert page.items[0].run_summary.model.scored == scored
    assert not (
        await browse_tasks_core(session, org_id="other-org", run_coverage="any")
    ).items


@pytest.mark.asyncio
async def test_preview_does_not_determine_coverage_or_average(session):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    task, version, org = await seed(
        session, [{"reward": 0.25, "created_at": old}] + [{"agent": "oracle"}] * 30
    )
    page = await browse_tasks_core(
        session, org_id=org, run_coverage="scored_model_trials"
    )
    item = page.items[0]
    assert len(item.latest_trials) == 24
    assert all(trial.agent == "oracle" for trial in item.latest_trials)
    assert item.run_summary.model.reward_sum == 0.25
    assert item.run_summary.model.scored == 1
    assert item.reward_sum == 30.25  # Installed clients retain the legacy population.
    assert item.reward_total == 31
    # A newer numbered version must not replace the chosen default's population.
    session.add(
        TaskVersionModel(
            id=f"{task}-v2", task_id=task, version=2, task_path="s3://test/v2"
        )
    )
    await refresh_task_browse_summaries(session, [f"{task}-v2"])
    assert (
        await browse_tasks_core(session, org_id=org, run_coverage="model_trials")
    ).items
    opened = await get_task_open_core(
        session, task_id=task, org_id=org, version_id=f"{task}-v2"
    )
    assert opened.selected_version.run_summary.model.total == 0
    await session.execute(
        update(TaskModel)
        .where(TaskModel.id == task)
        .values(current_version_id=f"{task}-v2")
    )
    assert not (
        await browse_tasks_core(session, org_id=org, run_coverage="model_trials")
    ).items
    assert (
        await browse_tasks_core(session, org_id=org, run_coverage="no_trials")
    ).items


@pytest.mark.asyncio
async def test_model_sorts_and_trial_changes(session):
    org = uuid.uuid4().hex
    a, av, _ = await seed(
        session,
        [
            {
                "reward": 0.25,
                "created_at": datetime.now(timezone.utc) - timedelta(days=1),
            }
        ],
        org=org,
    )
    b, bv, _ = await seed(session, [{"reward": 1}], org=org)
    c, _, _ = await seed(session, [{"agent": "oracle"}], org=org)
    for sort, expected in (
        ("model_score_asc", [a, b, c]),
        ("model_score_desc", [b, a, c]),
        ("last_model_activity_desc", [b, a, c]),
    ):
        page = await browse_tasks_core(session, org_id=org, sort=sort, limit=1)
        assert page.items[0].id == expected[0]
        assert (
            await browse_tasks_core(session, org_id=org, sort=sort, ids_only=True)
            == expected
        )
    # Resetting and then deleting a model execution removes its scored coverage.
    await session.execute(
        update(TrialModel)
        .where(TrialModel.task_id == b)
        .values(status=TrialStatus.PENDING, reward=None)
    )
    await refresh_task_browse_summaries(session, [bv])
    assert await browse_tasks_core(
        session, org_id=org, run_coverage="scored_model_trials", ids_only=True
    ) == [a]
    await session.execute(
        update(TrialModel)
        .where(TrialModel.task_id == b)
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await refresh_task_browse_summaries(session, [bv])
    assert await browse_tasks_core(
        session, org_id=org, run_coverage="no_trials", ids_only=True
    ) == [b]
    # A retry supersedes its predecessor; the same execution is not counted twice.
    await session.execute(
        update(TrialModel)
        .where(TrialModel.task_id == a)
        .values(superseded_by_trial_id=f"{b}-0")
    )
    await refresh_task_browse_summaries(session, [av])
    assert not await browse_tasks_core(
        session, org_id=org, run_coverage="model_trials", ids_only=True
    )


@pytest.mark.asyncio
async def test_frozen_migration_matches_live_refresh(session):
    specs = [
        {"agent": "oracle-test", "reward": 0.3},
        {"agent": "agent-nop", "reward": 0},
        {
            "reward": 0.5,
            "status": TrialStatus.FAILED,
            "error_message": "AgentTimeoutError",
        },
        {"status": TrialStatus.FAILED, "error_message": "harness", "reward": 1},
        {"reward": None},
        {"status": TrialStatus.PENDING, "reward": None},
        {"status": TrialStatus.SKIPPED},
        {"kind": "qa"},
        {"idempotency_key": "combine:copy"},
    ]
    _, version, _ = await seed(session, specs)
    empty_task, empty, empty_org = await seed(session)
    read = select(TaskBrowseSummaryModel.run_summary).where(
        TaskBrowseSummaryModel.task_version_id == version
    )
    expected = TaskRunSummary.model_validate((await session.execute(read)).scalar_one())
    await session.execute(
        update(TaskBrowseSummaryModel)
        .where(TaskBrowseSummaryModel.task_version_id.in_([version, empty]))
        .values(run_summary=None)
    )
    path = Path(__file__).parents[1] / "alembic/versions/task_run_coverage_001.py"
    spec = spec_from_file_location("run_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    # The frozen SQL predates the trials.is_probe column drop.
    backfill = migration._BACKFILL.replace("tr.is_probe IS NOT TRUE AND ", "")
    await session.execute(text(backfill), {"ids": [version, empty]})
    actual = TaskRunSummary.model_validate((await session.execute(read)).scalar_one())
    assert actual == expected
    zero = (
        await session.execute(
            select(TaskBrowseSummaryModel.run_summary).where(
                TaskBrowseSummaryModel.task_version_id == empty
            )
        )
    ).scalar_one()
    assert TaskRunSummary.model_validate(zero) == TaskRunSummary()
    # Missing historical payload is unknown, never classified as zero runs.
    await session.execute(
        update(TaskBrowseSummaryModel)
        .where(TaskBrowseSummaryModel.task_version_id == empty)
        .values(run_summary=None)
    )
    assert not await browse_tasks_core(
        session, org_id=empty_org, run_coverage="no_trials", ids_only=True
    )


@pytest.mark.asyncio
async def test_uploaded_versions_are_immediately_filterable_without_trials(
    session, monkeypatch
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from oddish.core import tasks

    @asynccontextmanager
    async def upload_session():
        yield session

    task_id = uuid.uuid4().hex
    monkeypatch.setattr(tasks, "get_session", upload_session)
    monkeypatch.setattr(
        tasks,
        "get_storage_client",
        lambda: type("Storage", (), {"object_exists": AsyncMock(return_value=True)})(),
    )
    monkeypatch.setattr(session, "commit", session.flush)
    monkeypatch.setattr(tasks.settings, "tasks_expand_archive", False)
    monkeypatch.setattr(tasks, "enqueue_tag_project_worker_job", AsyncMock())
    for version in (1, 2):
        await tasks.complete_task_upload(
            task_id=task_id,
            task_name=task_id,
            version=version,
            content_hash=f"hash-{version}",
            register=True,
            org_id=task_id,
        )
        page = await browse_tasks_core(
            session, org_id=task_id, run_coverage="no_trials"
        )
        assert page.items[0].id == task_id
        assert page.items[0].current_version == version
        assert page.items[0].run_summary == TaskRunSummary()


@pytest.mark.asyncio
async def test_coverage_reuses_browse_reads_and_open_stays_bounded(session):
    from sqlalchemy import event

    task, _, org = await seed(session, [{"reward": 0.5}])
    # Warm the unrelated cost-exclusion cache before measuring page reads.
    await browse_tasks_core(session, org_id=org)
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(session.bind.sync_engine, "after_cursor_execute", record)
    try:
        await browse_tasks_core(session, org_id=org)
        normal_count = len(statements)
        statements.clear()
        await browse_tasks_core(
            session,
            org_id=org,
            run_coverage="scored_model_trials",
            sort="model_score_desc",
        )
        assert len(statements) == normal_count
        statements.clear()
        opened = await get_task_open_core(session, task_id=task, org_id=org)
        assert len(statements) <= 3
        assert len(opened.model_dump_json().encode()) < 50_000
    finally:
        event.remove(session.bind.sync_engine, "after_cursor_execute", record)


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", [None, "avg_score_desc"])
async def test_browse_preview_projects_saved_effort_in_both_query_paths(session, sort):
    task, _, org = await seed(session, [
        {"harbor_config": {"agent_config": {"kwargs": {"reasoning_effort": "high"}}}},
        {"harbor_config": {"agent_overrides": {"kwargs": {"reasoning_effort": "low"}}}},
        {"harbor_config": {"agent_config": {"kwargs": {"reasoning_effort": None}}, "agent_overrides": {"kwargs": {"reasoning_effort": "low"}}}},
    ])
    page = await browse_tasks_core(session, org_id=org, sort=sort)
    trials = {trial.id: trial for trial in page.items[0].latest_trials}
    assert trials[f"{task}-0"].reasoning_effort == "high"
    assert trials[f"{task}-1"].reasoning_effort == "low"
    assert trials[f"{task}-2"].reasoning_effort is None
    assert all("harbor_config" not in trial.model_dump() for trial in trials.values())
