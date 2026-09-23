"""A verdict belongs to the task version whose evidence it evaluated.

Mirrors storage-multilog-commit-code: v4's baselines passed and its QA run
succeeded; v5's baselines then died on a worker heartbeat. The automatic v5
rejection must be shown on v5 only, and must not blame the verifier.
"""

from uuid import uuid4

import pytest

from oddish.core.endpoints.experiment_page import (
    _experiment_summary,
    _experiment_task_rows,
)
from oddish.core.endpoints.task_open import get_task_open_core
from oddish.core.endpoints.task_panel import get_task_panel_core
from oddish.db import TaskStatus, VerdictStatus, task_experiments
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)
from oddish.queue import maybe_start_task_qa_stage

HEARTBEAT = "Worker heartbeat stalled for over 15 minutes."


def _trial(task, experiment, version, **fields) -> TrialModel:
    return TrialModel(
        **{
            "id": uuid4().hex,
            "name": fields.get("agent", "trial"),
            "task_id": task.id,
            "task_version_id": version.id,
            "experiment_id": experiment.id,
            "org_id": task.org_id,
            "provider": "test",
            "queue_key": "test",
            **fields,
        }
    )


async def _seed(session):
    task = TaskModel(
        name=f"ownership-{uuid4().hex}",
        org_id="ownership-org",
        user="tester",
        task_path="s3://task",
        run_analysis=True,
        status=TaskStatus.RUNNING,
    )
    session.add(task)
    await session.flush()
    v4, v5 = (
        TaskVersionModel(
            id=f"{task.id}-v{n}",
            task_id=task.id,
            version=n,
            task_path=task.task_path,
            content_hash=f"hash-{n}",
            pre_trial={"items": []},
            pre_trial_status=VerdictStatus.SUCCESS,
        )
        for n in (4, 5)
    )
    session.add_all([v4, v5])
    v4_experiment = ExperimentModel(name=f"v4-{uuid4().hex}", org_id=task.org_id)
    v5_experiment = ExperimentModel(name=f"v5-{uuid4().hex}", org_id=task.org_id)
    session.add_all([v4_experiment, v5_experiment])
    await session.flush()
    session.add_all(
        [
            _trial(task, v4_experiment, v4, agent="nop", status=TrialStatus.SUCCESS, reward=0),
            _trial(task, v4_experiment, v4, agent="oracle", status=TrialStatus.SUCCESS, reward=1),
            _trial(task, v4_experiment, v4, agent="claude-code", kind="qa", status=TrialStatus.SUCCESS),
        ]
    )
    nop = _trial(
        task, v5_experiment, v5, agent="nop", status=TrialStatus.FAILED,
        reward=0, error_message=HEARTBEAT,
    )
    oracle = _trial(
        task, v5_experiment, v5, agent="oracle", status=TrialStatus.FAILED,
        reward=None, error_message=HEARTBEAT,
    )
    session.add_all([nop, oracle])
    for experiment in (v4_experiment, v5_experiment):
        await session.execute(
            task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
        )
    task.current_version_id = v5.id
    await session.flush()
    return task, v4, v5, v4_experiment, v5_experiment, oracle


async def _ownership(session, task, version, experiment):
    panel = await get_task_panel_core(
        session, task_id=task.id, version=version.version, org_id=task.org_id
    )
    opened = await get_task_open_core(
        session, task_id=task.id, version_id=version.id, org_id=task.org_id
    )
    row = (
        (
            await session.execute(
                _experiment_task_rows(experiment_id=experiment.id, org_id=task.org_id)
            )
        )
        .mappings()
        .one()
    )
    # The experiment row represents the version its trials ran.
    assert row["trial_version"] == version.version
    states = {
        panel.task.review_version_matches,
        opened.task.review_version_matches,
        row["review_version_matches"],
    }
    assert len(states) == 1, states
    return states.pop()


@pytest.mark.asyncio
async def test_v5_baseline_rejection_never_appears_on_v4(session):
    task, v4, v5, v4_experiment, v5_experiment, oracle = await _seed(session)

    admission = await maybe_start_task_qa_stage(session, task.id)

    assert admission.advanced and admission.task_version_id == v5.id
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict["is_good"] is False
    assert task.verdict["_task_version_id"] == v5.id
    # An unfinished run is reported as such, not as a broken verifier.
    issue = task.verdict["primary_issue"]
    assert issue.startswith("Baseline validation could not complete.")
    assert f"oracle trial {oracle.id} (failed): {HEARTBEAT}" in issue
    assert "Fix the nop/oracle" not in " ".join(task.verdict["recommendations"])

    assert await _ownership(session, task, v4, v4_experiment) is False
    assert await _ownership(session, task, v5, v5_experiment) is True
    summary, _ = await _experiment_summary(
        session, experiment_id=v4_experiment.id, org_id=task.org_id
    )
    assert summary.qa_rejected == 0

    # Production's stored verdict predates the version stamp and has no
    # grader. v4's successful QA run must not be credited with it.
    task.verdict = {
        k: v for k, v in task.verdict.items() if k != "_task_version_id"
    }
    await session.flush()
    assert await _ownership(session, task, v4, v4_experiment) is False
    assert await _ownership(session, task, v5, v5_experiment) is False
