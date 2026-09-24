"""Real Postgres submission boundaries for ABT-1335 (uses ODDISH_DATABASE_URL)."""

import uuid

import pytest
from sqlalchemy import select

from oddish.config import settings
from oddish.db import TaskModel, TrialModel, WorkerJobModel, get_session
from oddish.queue import append_trials_to_task, create_task
from oddish.schemas import TaskSubmission, TrialSpec
from oddish.workers.queue.trial_steps import prepare_trial_run


@pytest.mark.asyncio
@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize(
    "direct,bedrock,initial_flag",
    [
        ("anthropic/claude-opus-4-8", "bedrock/global.anthropic.claude-opus-4-8", True),
        ("claude-opus-4-8", "bedrock/global.anthropic.claude-opus-4-8", True),
        ("anthropic/claude-opus-4-8", "claude-opus-4-8", False),
    ],
)
async def test_submitted_provider_is_persisted_in_trial_and_worker_job(
    append, direct, bedrock, initial_flag, monkeypatch
):
    # No model/provider helpers are mocked: exercise the same create and append
    # transactions the server uses, including worker-job enqueueing.
    monkeypatch.setattr(settings, "claude_code_force_direct_api", initial_flag)
    task_id = "routing-" + uuid.uuid4().hex[:12]
    submission = TaskSubmission(
        name=task_id,
        task_path="s3://test-bucket/routing/task.tar.gz",
        user="routing-test",
        trials=[
            TrialSpec(agent="claude-code", model=direct),
            TrialSpec(agent="mini-swe-agent", model=bedrock),
        ],
    )
    try:
        async with get_session() as session:
            initial = (
                submission.model_copy(update={"trials": []}) if append else submission
            )
            await create_task(session, initial, task_id=task_id)
        if append:
            async with get_session() as session:
                task = await session.get(TaskModel, task_id)
                await append_trials_to_task(session, task=task, submission=submission)
        async with get_session() as session:
            records = (
                await session.execute(
                    select(TrialModel, WorkerJobModel)
                    .join(WorkerJobModel, WorkerJobModel.subject_id == TrialModel.id)
                    .where(
                        TrialModel.task_id == task_id,
                        TrialModel.kind == "agent",
                        WorkerJobModel.kind == "TRIAL",
                    )
                )
            ).all()
            actual = {
                (t.agent, t.model, t.provider, t.queue_key, j.queue_key)
                for t, j in records
            }
            assert actual == {
                (
                    "claude-code",
                    "anthropic/claude-opus-4-8",
                    "anthropic",
                    "anthropic/claude-opus-4-8",
                    "anthropic/claude-opus-4-8",
                ),
                (
                    "mini-swe-agent",
                    "global.anthropic.claude-opus-4-8",
                    "bedrock",
                    "global.anthropic.claude-opus-4-8",
                    "global.anthropic.claude-opus-4-8",
                ),
            }
            trial_ids = [t.id for t, _ in records]

        # Worker attempts normalize again. A changed default must not move
        # already submitted work to another transport or queue, even on retry.
        for force_direct in (True, False):
            monkeypatch.setattr(settings, "claude_code_force_direct_api", force_direct)
            for trial_id in trial_ids:
                prepared = await prepare_trial_run(
                    trial_id=trial_id,
                    worker_id="routing-test",
                    queue_slot=0,
                    modal_function_call_id=None,
                )
                assert prepared is not None
            async with get_session() as session:
                records = (
                    await session.execute(
                        select(TrialModel, WorkerJobModel)
                        .join(
                            WorkerJobModel, WorkerJobModel.subject_id == TrialModel.id
                        )
                        .where(
                            TrialModel.id.in_(trial_ids), WorkerJobModel.kind == "TRIAL"
                        )
                    )
                ).all()
                assert {
                    (t.agent, t.model, t.provider, t.queue_key, j.queue_key)
                    for t, j in records
                } == actual
    finally:
        async with get_session() as session:
            trial_ids = select(TrialModel.id).where(TrialModel.task_id == task_id)
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.in_(trial_ids)
                )
            )
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == task_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
