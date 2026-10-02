"""Real Postgres insertion and monitor parsing for old and new worker job IDs."""

from uuid import UUID

import pytest
from sqlalchemy import select

from oddish.db import WorkerJobModel, WorkerJobKind
from oddish.workers.jobs.enqueue import (
    EnqueueRequest,
    bulk_enqueue_worker_jobs,
    enqueue_worker_job,
)
from oddish.workers.launcher.token import format_credential, parse_credential


@pytest.mark.asyncio
async def test_single_bulk_and_model_default_keep_legacy_parent_links(session):
    parent = WorkerJobModel(
        id="1234abcd", kind=WorkerJobKind.TRIAL, queue_key="identity-test"
    )
    session.add(parent)
    await session.flush()
    single = await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL, queue_key="identity-test", parent_job_id=parent.id
        ),
        validate=False,
    )
    bulk = await bulk_enqueue_worker_jobs(
        session,
        [
            EnqueueRequest(
                kind=WorkerJobKind.TRIAL,
                queue_key="identity-test",
                parent_job_id=single.id,
            )
            for _ in range(100)
        ],
        validate=False,
    )
    for job_id in [single.id, *bulk]:
        assert len(job_id) == 32 and UUID(hex=job_id).version == 4
        assert parse_credential(format_credential(job_id, 2, "secret")) == (
            job_id,
            2,
            "secret",
        )
    assert len(set(bulk)) == 100
    rows = (
        (
            await session.execute(
                select(WorkerJobModel).where(WorkerJobModel.id.in_(bulk))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 100
    assert all(row.parent_job_id == single.id for row in rows)
    assert (await session.get(WorkerJobModel, single.id)).parent_job_id == "1234abcd"
    default = WorkerJobModel(kind=WorkerJobKind.TRIAL, queue_key="identity-test")
    session.add(default)
    await session.flush()
    assert len(default.id) == 32
