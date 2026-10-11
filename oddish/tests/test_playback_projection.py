"""Playback metadata reaches the first response without loading the full result."""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import load_only

from oddish.core.endpoints.experiment_page import (
    _experiment_trial_projection,
    build_experiment_trial_cell,
)
from oddish.core.helpers import SLIM_TRIAL_RESPONSE_COLUMNS, build_slim_trial_response
from oddish.core.playback import absent_descriptor, invalid_descriptor, ready_descriptor
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, TrialStatus
from oddish.schemas import TrialPlayback


@pytest.mark.asyncio
@pytest.mark.parametrize("playback", [
    None,
    {"manifest_path": None},
    {"manifest_path": "verifier/artifacts/playback.json"},
    absent_descriptor(),
    ready_descriptor("verifier/artifacts/playback.json", "f" * 64),
    invalid_descriptor("verifier/artifacts/playback.json", "missing_reference"),
])
async def test_playback_projection_keeps_unknown_absent_and_present_distinct(
    session, playback
):
    identity = uuid.uuid4().hex
    task = TaskModel(name=identity, org_id=identity, user="tester", task_path="robot")
    experiment = ExperimentModel(name=identity, org_id=identity)
    session.add_all([task, experiment])
    await session.flush()
    session.add(TrialModel(
        id=identity,
        name=identity,
        task_id=task.id,
        org_id=identity,
        experiment_id=experiment.id,
        agent="oracle",
        provider="local",
        queue_key="oracle",
        status=TrialStatus.SUCCESS,
        input_tokens=5_714_326_425,
        output_tokens=42,
        total_steps=17,
        result={
            "large_metric": "x" * 100_000,
            **({"_playback": playback} if playback is not None else {}),
        },
    ))
    await session.flush()
    session.expunge_all()

    trial = (await session.execute(
        select(TrialModel)
        .where(TrialModel.id == identity)
        .options(load_only(*SLIM_TRIAL_RESPONSE_COLUMNS, TrialModel.harbor_config))
    )).scalar_one()
    assert "result" not in trial.__dict__
    # Older results omit the index fields; responses then report them as null.
    expected = None if playback is None else {**TrialPlayback().model_dump(), **playback}
    response = build_slim_trial_response(
        trial, "robot", analysis=None, error_message=None
    )
    assert response.model_dump()["playback"] == expected
    assert response.input_tokens == 5_714_326_425

    row = (await session.execute(
        _experiment_trial_projection(TrialModel).where(TrialModel.id == identity)
    )).mappings().one()
    assert "result" not in row
    cell = build_experiment_trial_cell(row, exclusions=None)
    assert cell.model_dump()["playback"] == expected
    assert cell.input_tokens == 5_714_326_425
    assert cell.total_steps == 17
