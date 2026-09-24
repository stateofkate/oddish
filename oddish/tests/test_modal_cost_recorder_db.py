from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from oddish.costs.modal_cost import SpanResources
from oddish.costs.recorder import (
    WorkerBillingSpec,
    close_agent_sandboxes,
    close_worker_span,
    open_worker_span,
    reconcile_compute_cost_spans,
    transition_agent_sandbox,
)
from oddish.db import (
    ExperimentModel,
    ModalCostSpanModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
)


def _sandbox_resources() -> SpanResources:
    return SpanResources(
        cpu_request=2,
        cpu_limit=2,
        mem_request_mb=4096,
        mem_limit_mb=None,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=Decimal(1),
        container_class="sandbox",
        spec_source="pinned",
        cpu_enforcement_mode="auto",
        mem_enforcement_mode="auto",
    )


async def _seed() -> tuple[str, str, str, str]:
    suffix = uuid.uuid4().hex[:10]
    experiment_id = f"modal-exp-{suffix}"
    task_id = f"modal-task-{suffix}"
    trial_id = f"modal-trial-{suffix}"
    worker_job_id = f"modal-job-{suffix}"
    async with get_session() as session:
        session.add(
            ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-modal")
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id="org-modal",
                user="tester",
                task_path="/tmp/modal-cost-test",
                status=TaskStatus.RUNNING,
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                experiment_id=experiment_id,
                org_id="org-modal",
                billed_user_id="user-modal",
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                max_attempts=6,
                attempts=2,
                status=TrialStatus.RUNNING,
            )
        )
        session.add(
            WorkerJobModel(
                id=worker_job_id,
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_id,
                attempts=3,
                current_worker_id="worker-modal",
            )
        )
    return experiment_id, task_id, trial_id, worker_job_id


async def _remove(ids: tuple[str, str, str, str]) -> None:
    experiment_id, task_id, trial_id, worker_job_id = ids
    async with get_session() as session:
        await session.execute(
            ModalCostSpanModel.__table__.delete().where(
                ModalCostSpanModel.worker_job_id == worker_job_id
            )
        )
        await session.execute(
            WorkerJobModel.__table__.delete().where(WorkerJobModel.id == worker_job_id)
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )


@requires_db
@pytest.mark.asyncio
async def test_worker_and_internal_retry_spans_are_idempotent_and_priced() -> None:
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=worker_job_id,
        attempts=3,
        subject_table="trials",
        subject_id=trial_id,
    )
    try:
        spec = WorkerBillingSpec(cpu_cores=1, memory_mb=3072, nonpreemptible=True)
        await open_worker_span(job, spec, started_at=t0)
        await open_worker_span(job, spec, started_at=t0 + timedelta(seconds=1))
        await close_worker_span(
            worker_job_id, 3, finished_at=t0 + timedelta(seconds=60)
        )

        common = dict(
            worker_job_id=worker_job_id,
            worker_job_attempt=3,
            trial_id=trial_id,
            attempt=2,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            resources=_sandbox_resources(),
        )
        await transition_agent_sandbox(
            **common, external_id=f"sb-{worker_job_id}-1", observed_at=t0
        )
        await transition_agent_sandbox(
            **common,
            external_id=f"sb-{worker_job_id}-2",
            observed_at=t0 + timedelta(seconds=20),
        )
        await close_agent_sandboxes(
            worker_job_id, 3, finished_at=t0 + timedelta(seconds=50)
        )

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel)
                        .where(ModalCostSpanModel.worker_job_id == worker_job_id)
                        .order_by(
                            ModalCostSpanModel.component_role,
                            ModalCostSpanModel.span_ordinal,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [row.component_role for row in rows] == [
            "agent_sandbox",
            "agent_sandbox",
            "worker_function",
        ]
        assert rows[0].finished_at == t0 + timedelta(seconds=20)
        assert rows[1].finished_at == t0 + timedelta(seconds=50)
        assert rows[0].span_ordinal == 0
        assert rows[1].span_ordinal == 1
        assert all(row.cost_usd is not None and row.cost_usd > 0 for row in rows)
        assert rows[2].price_multiplier == Decimal(3)
        assert rows[2].trial_id == trial_id
    finally:
        await _remove(ids)


@requires_db
@pytest.mark.asyncio
async def test_terminal_reconciliation_uses_trial_heartbeat_fallback() -> None:
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    heartbeat = t0 + timedelta(seconds=41)
    try:
        await transition_agent_sandbox(
            worker_job_id=worker_job_id,
            worker_job_attempt=3,
            trial_id=trial_id,
            attempt=2,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            external_id=f"sb-{worker_job_id}",
            resources=_sandbox_resources(),
            observed_at=t0,
        )
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == worker_job_id)
                .values(status=WorkerJobStatus.CANCELLED, finished_at=None)
            )
            await session.execute(
                update(TrialModel)
                .where(TrialModel.id == trial_id)
                .values(heartbeat_at=heartbeat)
            )

        assert await reconcile_compute_cost_spans() == 1
        assert await reconcile_compute_cost_spans() == 0
        async with get_session() as session:
            row = await session.scalar(
                select(ModalCostSpanModel).where(
                    ModalCostSpanModel.worker_job_id == worker_job_id
                )
            )
        assert row is not None
        assert row.finished_at == heartbeat
        assert row.basis == "reconciled"
        assert row.cost_usd is not None and row.cost_usd > 0
    finally:
        await _remove(ids)


@requires_db
@pytest.mark.asyncio
async def test_reconcile_does_not_bill_stale_attempt_through_later_finished_at() -> (
    None
):
    # A span left open by attempt 1 must not be closed at the job's terminal
    # finished_at (which belongs to a later attempt). Its true end is unknown,
    # so it is recorded unpriced (cost NULL + reason) rather than billed through
    # the later attempt's runtime or falsely priced at zero.
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    job_finished = t0 + timedelta(hours=1)  # end of a LATER attempt
    try:
        await transition_agent_sandbox(
            worker_job_id=worker_job_id,
            worker_job_attempt=1,  # stale: the job is on attempt 3 (see _seed)
            trial_id=trial_id,
            attempt=1,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            external_id=f"sb-stale-{worker_job_id}",
            resources=_sandbox_resources(),
            observed_at=t0,
        )
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == worker_job_id)
                .values(status=WorkerJobStatus.FAILED, finished_at=job_finished)
            )

        assert await reconcile_compute_cost_spans() == 1
        async with get_session() as session:
            row = await session.scalar(
                select(ModalCostSpanModel).where(
                    ModalCostSpanModel.worker_job_id == worker_job_id
                )
            )
        assert row is not None
        assert row.finished_at == t0  # closed (out of the open set), NOT job_finished
        assert row.cost_usd is None  # unknown cost -> NULL, never a false 0
        assert row.unpriced_reason == "stale_attempt"
        assert row.basis == "reconciled"
    finally:
        await _remove(ids)


@requires_db
@pytest.mark.asyncio
async def test_cost_spans_keep_full_uuid_when_prefixes_collide(monkeypatch):
    from oddish.db import init_db
    from oddish.db import models
    from oddish.costs.modal_cost import build_span_row

    await init_db()
    ids = [
        uuid.UUID("4806cb75-1111-4111-8111-111111111111"),
        uuid.UUID("4806cb75-2222-4222-8222-222222222222"),
    ]
    generated = iter(ids)
    monkeypatch.setattr(models, "uuid4", lambda: next(generated))
    # Roll back so deterministic IDs do not persist across repeated runs.
    async with get_session() as session:
        rows = [
            build_span_row(
                component_role="worker_function",
                provider="modal",
                started_at=datetime.now(timezone.utc),
                basis="hooks",
                resources=_sandbox_resources(),
            )
            for _ in ids
        ]
        session.add_all(rows)
        await session.flush()
        assert [row.id for row in rows] == [value.hex for value in ids]
        await session.rollback()


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mb", [3072, 1536])
async def test_candidate_attempt_cost_uses_its_cpu_and_scalar_memory(memory_mb):
    ids = await _seed()
    _, _, trial_id, worker_job_id = ids
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=worker_job_id, attempts=3, subject_table="trials", subject_id=trial_id
    )
    try:
        candidate = WorkerBillingSpec(
            0.6,
            memory_mb,
            True,
            cpu_limit=17,
            configuration=f"candidate-cpu0.6-mem{memory_mb}",
        )
        await open_worker_span(job, candidate, started_at=start)
        await close_worker_span(
            worker_job_id, 3, finished_at=start + timedelta(seconds=60)
        )
        job.attempts = 4
        await open_worker_span(job, WorkerBillingSpec(1, 3072, True), started_at=start)
        await close_worker_span(
            worker_job_id, 4, finished_at=start + timedelta(seconds=60)
        )
        async with get_session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ModalCostSpanModel)
                        .where(ModalCostSpanModel.worker_job_id == worker_job_id)
                        .order_by(ModalCostSpanModel.worker_job_attempt)
                    )
                ).all()
            )
        test, base = rows
        assert (
            test.cpu_request,
            test.cpu_limit,
            test.mem_request_mb,
            test.mem_limit_mb,
        ) == (0.6, 17, memory_mb, None)
        assert test.price_multiplier == 3
        assert test.cost_usd > 0 and test.cost_usd < base.cost_usd
        assert base.cpu_request == 1 and base.mem_request_mb == 3072
    finally:
        await _remove(ids)


# The report's query: warm-up spend per task version, from the trial that
# triggered each warm-up.
WARMUP_COST_PER_TASK_VERSION = """
SELECT t.task_version_id, count(*) AS warmups, sum(c.cost_usd) AS warmup_usd
FROM modal_costs c JOIN trials t ON t.id = c.trial_id
WHERE c.component_role = 'warmup_sandbox' AND c.deleted_at IS NULL
GROUP BY t.task_version_id
"""


@requires_db
@pytest.mark.asyncio
async def test_warm_up_spans_are_priced_per_trial_and_task_version(monkeypatch):
    from sqlalchemy import text

    from oddish.db import TaskVersionModel, init_db
    from oddish.workers.launcher import costs as monitor_costs

    await init_db()
    ids = await _seed()
    experiment_id, task_id, trial_id, worker_job_id = ids
    version_id = f"{task_id}-v1"
    t0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
    clock = iter(t0 + timedelta(seconds=s) for s in (0, 60, 90, 390, 400))
    monkeypatch.setattr(monitor_costs, "utcnow", lambda: next(clock))
    try:
        async with get_session() as session:
            session.add(
                TaskVersionModel(
                    id=version_id, task_id=task_id, version=1, task_path="/tmp/v1"
                )
            )
            await session.flush()
            await session.execute(
                update(TrialModel)
                .where(TrialModel.id == trial_id)
                .values(task_version_id=version_id)
            )
        prepared = SimpleNamespace(
            trial_attempt=2, experiment_id=experiment_id, org_id="org-modal",
            billed_user_id="user-modal",
        )  # fmt: skip
        span = monitor_costs.WarmUpSpan(
            job_id=worker_job_id, job_attempt=3, trial_id=trial_id,
            prepared=prepared, provider="modal", resources=_sandbox_resources(),
        )  # fmt: skip
        # A failed warm-up (60 s), then its retry (300 s), each its own span.
        await span.started(f"sb-warm-{worker_job_id}-1")
        await span.ended()
        await span.started(f"sb-warm-{worker_job_id}-2")
        await span.ended()
        # The trial's own sandbox is a separate role, left alone by the close.
        await transition_agent_sandbox(
            worker_job_id=worker_job_id, worker_job_attempt=3, trial_id=trial_id,
            attempt=2, experiment_id=experiment_id, org_id="org-modal",
            billed_user_id="user-modal", provider="modal",
            external_id=f"sb-{worker_job_id}", resources=_sandbox_resources(),
            observed_at=next(clock),
        )  # fmt: skip

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel)
                        .where(ModalCostSpanModel.worker_job_id == worker_job_id)
                        .order_by(
                            ModalCostSpanModel.component_role,
                            ModalCostSpanModel.span_ordinal,
                        )
                    )
                )
                .scalars()
                .all()
            )
            per_version = (
                await session.execute(text(WARMUP_COST_PER_TASK_VERSION))
            ).all()
        agent, *warmups = rows
        assert agent.component_role == "agent_sandbox" and agent.finished_at is None
        assert [w.component_role for w in warmups] == ["warmup_sandbox"] * 2
        assert [w.span_ordinal for w in warmups] == [0, 1]
        assert [w.external_id for w in warmups] == [
            f"sb-warm-{worker_job_id}-1",
            f"sb-warm-{worker_job_id}-2",
        ]
        assert [(w.finished_at - w.started_at).seconds for w in warmups] == [60, 300]
        for w in warmups:
            assert (w.trial_id, w.worker_job_attempt, w.attempt) == (trial_id, 3, 2)
            assert (w.experiment_id, w.billed_user_id) == (experiment_id, "user-modal")
            assert w.cost_usd is not None and w.cost_usd > 0
        # 2 cores and 4 GiB of Modal sandbox per second, stored at 6 places.
        per_sec = 2 * Decimal("0.00003942") + 4 * Decimal("0.00000667")
        priced = [(s * per_sec).quantize(Decimal("0.000001")) for s in (60, 300)]
        assert [w.cost_usd for w in warmups] == priced
        expected = sum(priced)
        mine = [r for r in per_version if r.task_version_id == version_id]
        assert [(r.warmups, r.warmup_usd) for r in mine] == [(2, expected)]
    finally:
        await _remove(ids)
