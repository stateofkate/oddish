from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from oddish.core.dashboard import (
    _build_aggregates_for_experiment_ids,
    _experiment_row_passes_status_filter,
)
from oddish.core.verdict_state import INSUFFICIENT_EVIDENCE_ERROR
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    generate_id,
    task_experiments,
)


def _task(name: str, *, verdict_error: str, run_analysis: bool = True) -> TaskModel:
    return TaskModel(
        name=name,
        org_id="org1",
        user="tester",
        task_path=f"s3://tasks/{name}",
        run_analysis=run_analysis,
        verdict_status=VerdictStatus.FAILED,
        verdict_error=verdict_error,
    )


def _trial(task: TaskModel, experiment: ExperimentModel) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id="org1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=TrialStatus.SUCCESS,
    )


async def _task_aggregates(session, experiment, tasks):
    session.add_all([*tasks, experiment])
    await session.flush()
    session.add_all([_trial(task, experiment) for task in tasks])
    await session.execute(
        insert(task_experiments).values(
            [{"task_id": task.id, "experiment_id": experiment.id} for task in tasks]
        )
    )
    await session.flush()

    task_agg, _, _ = _build_aggregates_for_experiment_ids(
        [experiment.id], org_id="org1"
    )
    return (
        (
            await session.execute(
                select(task_agg).where(task_agg.c.experiment_id == experiment.id)
            )
        )
        .mappings()
        .one()
    )


@pytest.mark.asyncio
async def test_missing_evidence_and_cancellation_count_as_pending_not_failed(session):
    no_evidence = _task(
        "verdict-bucket-no-evidence", verdict_error=INSUFFICIENT_EVIDENCE_ERROR
    )
    cancelled = _task("verdict-bucket-cancelled", verdict_error="Cancelled by user")
    crashed = _task("verdict-bucket-crashed", verdict_error="worker crashed")
    experiment = ExperimentModel(name="verdict-buckets", org_id="org1")
    row = await _task_aggregates(session, experiment, [no_evidence, cancelled, crashed])
    assert row["task_count"] == 3
    assert row["verdict_failed"] == 1
    assert row["verdict_pending"] == 2

    only_missing_evidence = {**row, "verdict_failed": 0, "failed_trials": 0}
    assert _experiment_row_passes_status_filter(
        only_missing_evidence, status_filter="pending-verdict"
    )
    assert not _experiment_row_passes_status_filter(
        only_missing_evidence, status_filter="failed"
    )


@pytest.mark.asyncio
async def test_cancelled_qa_on_legacy_no_analysis_task_counts_as_pending(session):
    # Tasks created before analysis became unconditional keep run_analysis
    # false; an explicit QA re-run that is then cancelled must not leave both
    # the pending and failed buckets.
    cancelled = _task(
        "verdict-bucket-legacy-cancelled",
        verdict_error="Cancelled by user",
        run_analysis=False,
    )
    experiment = ExperimentModel(name="verdict-buckets-legacy", org_id="org1")
    row = await _task_aggregates(session, experiment, [cancelled])
    assert row["verdict_failed"] == 0
    assert row["verdict_pending"] == 1
