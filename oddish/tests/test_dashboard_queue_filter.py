from uuid import uuid4

import pytest
from sqlalchemy import insert

from oddish.core.dashboard import load_dashboard_experiments
from oddish.db import experiment_trials
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)

ORG = "queue-filter-org"


async def _experiment_with_job(session, *, queue_key, job_status, shadow=False):
    """Experiment (or a shadow under it) holding one trial with one job."""
    task = TaskModel(
        name=f"qf-{uuid4().hex}", org_id=ORG, user="tester", task_path="s3://t"
    )
    experiment = ExperimentModel(name=f"qf-exp-{uuid4().hex}", org_id=ORG)
    session.add_all([task, experiment])
    await session.flush()
    owner = experiment
    if shadow:
        owner = ExperimentModel(
            name=f"qf-shadow-{uuid4().hex}", org_id=ORG, shadow_of=experiment.id
        )
        session.add(owner)
        await session.flush()
    trial = TrialModel(
        id=uuid4().hex,
        name="solver",
        task_id=task.id,
        experiment_id=owner.id,
        org_id=ORG,
        agent="codex",
        provider="openai",
        queue_key=queue_key,
        status=TrialStatus.QUEUED,
    )
    session.add(trial)
    await session.flush()
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=job_status,
            subject_table="trials",
            subject_id=trial.id,
            queue_key=queue_key,
            org_id=ORG,
        )
    )
    await session.flush()
    return experiment.id, trial.id


async def _collection_of(session, trial_id):
    """A read-only collection gathering ``trial_id`` (an experiment_trials row)."""
    collection = ExperimentModel(name=f"qf-coll-{uuid4().hex}", org_id=ORG)
    session.add(collection)
    await session.flush()
    await session.execute(
        insert(experiment_trials).values(experiment_id=collection.id, trial_id=trial_id)
    )
    return collection.id


async def _listed(session, queue, state=None):
    rows, _ = await load_dashboard_experiments(
        session,
        org_id=ORG,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="all",
        experiments_queue=queue,
        experiments_queue_state=state,
    )
    return {row["id"] for row in rows}


@pytest.mark.asyncio
async def test_queue_filter_matches_active_jobs_shadows_and_collections(session):
    queued, queued_trial = await _experiment_with_job(
        session, queue_key="q/a", job_status=WorkerJobStatus.RETRYING
    )
    running, _ = await _experiment_with_job(
        session, queue_key="q/a", job_status=WorkerJobStatus.RUNNING
    )
    qa_parent, _ = await _experiment_with_job(
        session, queue_key="q/a", job_status=WorkerJobStatus.QUEUED, shadow=True
    )
    finished, finished_trial = await _experiment_with_job(
        session, queue_key="q/a", job_status=WorkerJobStatus.SUCCESS
    )
    other_queue, _ = await _experiment_with_job(
        session, queue_key="q/b", job_status=WorkerJobStatus.RUNNING
    )
    # Collections match through the gathered trial's job, not any trial.
    live_collection = await _collection_of(session, queued_trial)
    await _collection_of(session, finished_trial)

    assert await _listed(session, "q/a") == {
        queued,
        running,
        qa_parent,
        live_collection,
    }
    assert await _listed(session, "q/a", "queued") == {
        queued,
        qa_parent,
        live_collection,
    }
    assert await _listed(session, "q/a", "running") == {running}
    assert await _listed(session, "q/b") == {other_queue}
    # Without the queue filter the finished experiment lists like any other.
    assert finished in await _listed(session, None)


@pytest.mark.asyncio
async def test_queue_filter_matches_stored_aliases_of_the_key(session):
    # Queue Health shows both jobs on one normalized row, openai/gpt-5.
    bare, _ = await _experiment_with_job(
        session, queue_key="gpt-5", job_status=WorkerJobStatus.QUEUED
    )
    canonical, _ = await _experiment_with_job(
        session, queue_key="openai/gpt-5", job_status=WorkerJobStatus.RUNNING
    )

    assert await _listed(session, "openai/gpt-5") == {bare, canonical}
    assert await _listed(session, "gpt-5") == {bare, canonical}
    assert await _listed(session, "openai/gpt-5", "queued") == {bare}
    assert await _listed(session, "openai/gpt-4") == set()
