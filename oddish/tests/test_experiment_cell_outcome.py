"""Grid cells omit error_message, so they carry the task page's outcome rule."""

import uuid

import pytest

from oddish.core.endpoints.experiment_page import (
    _experiment_trial_rows_query,
    build_experiment_trial_cell,
)
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, TrialStatus

# name: (status, reward, error_message, outcome)
CASES = {
    "graded-timeout": (
        TrialStatus.FAILED,
        0.5,
        "AgentTimeoutError: out of time",
        "partial",
    ),
    "harness-after-success": (TrialStatus.SUCCESS, 1.0, "sandbox died", "harness"),
    "clean-pass": (TrialStatus.SUCCESS, 1.0, None, "pass"),
}


@pytest.mark.asyncio
async def test_grid_cells_carry_the_server_outcome(session):
    identity = uuid.uuid4().hex
    task = TaskModel(name=identity, org_id=identity, user="tester", task_path="robot")
    experiment = ExperimentModel(name=identity, org_id=identity)
    session.add_all([task, experiment])
    await session.flush()
    session.add_all(
        TrialModel(
            id=f"{identity}-{name}",
            name=name,
            task_id=task.id,
            org_id=identity,
            experiment_id=experiment.id,
            agent="claude-code",
            provider="anthropic",
            queue_key="anthropic",
            status=status,
            reward=reward,
            error_message=error,
        )
        for name, (status, reward, error, _) in CASES.items()
    )
    await session.flush()

    query, _ = _experiment_trial_rows_query(
        experiment_id=experiment.id, org_id=identity
    )
    rows = (await session.execute(query)).mappings().all()
    assert "error_message" not in rows[0]
    cells = [build_experiment_trial_cell(row, exclusions=None) for row in rows]
    assert {cell.name: cell.outcome for cell in cells} == {
        name: outcome for name, (*_, outcome) in CASES.items()
    }
