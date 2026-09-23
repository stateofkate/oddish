"""Compute-cost accounting for a monitor-driven attempt.

The worker path opens an ``agent_sandbox`` span when Harbor reports its sandbox
and closes and prices it when the trial ends. Here the billable sandbox is the
outer one the launcher starts, so the launcher opens the span and settlement
closes it, through the same recorder and ``settle_compute_costs``.

Spans are keyed by job and attempt in the database. Only what the ledger cannot
hold (the provider and the verifier's resources) rides on the job's payload.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from decimal import Decimal
from typing import Any

from sqlalchemy import literal, update
from sqlalchemy.dialects.postgresql import JSONB

from oddish.costs.modal_cost import SpanResources
from oddish.costs.recorder import transition_agent_sandbox
from oddish.db import WorkerJobModel, WorkerJobStatus, get_session
from oddish.db.models import utcnow
from oddish.workers.console import console
from oddish.workers.queue.trial_handler import PreparedTrialRun, SandboxCostState

PAYLOAD_KEY = "monitor_cost"


def _dump(resources: SpanResources | None) -> dict[str, Any] | None:
    if resources is None:
        return None
    return {**asdict(resources), "price_multiplier": str(resources.price_multiplier)}


def _load(data: dict[str, Any] | None) -> SpanResources | None:
    if not data:
        return None
    known = {field.name for field in fields(SpanResources)}
    values = {key: value for key, value in data.items() if key in known}
    values["price_multiplier"] = Decimal(str(values.get("price_multiplier", "1")))
    return SpanResources(**values)


async def open_sandbox_span(
    *,
    job_id: str,
    job_attempt: int,
    worker_id: str,
    trial_id: str,
    prepared: PreparedTrialRun,
    provider: str,
    external_id: str,
    resources: SpanResources,
    verifier_resources: SpanResources | None,
) -> None:
    """Best effort, like the worker path: accounting never fails an attempt."""
    try:
        patch = {
            PAYLOAD_KEY: {
                "provider": provider,
                "resources": _dump(resources),
                "verifier_resources": _dump(verifier_resources),
            }
        }
        # One statement fenced like every launcher write (monitor_launch's
        # _owned_by, not imported: that module imports this one). A reaped or
        # re-claimed row is left alone, so the new attempt keeps its reset.
        async with get_session() as session:
            result = await session.execute(
                update(WorkerJobModel)
                .where(
                    WorkerJobModel.id == job_id,
                    WorkerJobModel.status == WorkerJobStatus.RUNNING,
                    WorkerJobModel.attempts == job_attempt,
                    WorkerJobModel.current_worker_id == worker_id,
                )
                .values(payload=WorkerJobModel.payload.op("||")(literal(patch, JSONB)))
            )
        if result.rowcount != 1:
            console.print(
                f"[yellow]Trial {trial_id} compute-cost open skipped: job "
                f"{job_id} is no longer this worker's RUNNING attempt {job_attempt}[/yellow]"
            )
            return
        await transition_agent_sandbox(
            worker_job_id=job_id,
            worker_job_attempt=job_attempt,
            trial_id=trial_id,
            attempt=prepared.trial_attempt,
            experiment_id=prepared.experiment_id or None,
            org_id=prepared.org_id,
            billed_user_id=prepared.billed_user_id,
            provider=provider,
            external_id=external_id,
            resources=resources,
            observed_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]Trial {trial_id} compute-cost open failed: {exc}[/yellow]"
        )


def cost_state_for_settle(
    *,
    payload: dict[str, Any] | None,
    job_id: str,
    job_attempt: int,
    trial_id: str,
    prepared: PreparedTrialRun,
) -> SandboxCostState | None:
    """The state ``settle_compute_costs`` needs, rebuilt from the job row.
    ``None`` when the launcher never opened a span (nothing to close)."""
    stored = (payload or {}).get(PAYLOAD_KEY)
    resources = _load((stored or {}).get("resources"))
    if not stored or resources is None:
        return None
    return SandboxCostState(
        resources=resources,
        # The verifier ran inside the sandbox the outer span already prices.
        verifier_resources=None,
        provider=stored.get("provider") or "",
        trial_id=trial_id,
        attempt=prepared.trial_attempt,
        experiment_id=prepared.experiment_id or None,
        org_id=prepared.org_id,
        billed_user_id=prepared.billed_user_id,
        worker_job_id=job_id,
        worker_job_attempt=job_attempt,
        task_id=prepared.task_id,
        task_version_id=prepared.task_version_id,
        # The outer sandbox is stopped at settlement, so that is when it ends.
        terminal_at=utcnow(),
    )
