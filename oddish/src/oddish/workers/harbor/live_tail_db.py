"""The database side of the live tail: what a persist tick writes on the worker.

``live_tail`` itself never touches the database; it takes a ``PersistTick``
callback. The worker passes ``persist_tick_to_database``; the in-sandbox
monitor passes one that posts to the control plane.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oddish.db import TrialEventModel, TrialModel
from oddish.core.progress_history import retain_setup_events
from oddish.db.connection import get_session
from oddish.workers.console import console
from oddish.workers.harbor.live_tail import PersistResult


async def persist_tick_to_database(
    *,
    trial_id: str,
    attempt: int,
    events: list[dict[str, Any]],
    checkpoint_values: dict[str, Any] | None,
    org_id: str | None,
    billed_user_id: str | None,
) -> PersistResult:
    """Insert live rows, write the usage checkpoint, then enforce quotas."""
    flushed = 0
    checkpoint_written = False
    async with get_session() as session:
        if events:
            await retain_setup_events(
                session, trial_id=trial_id, attempt=attempt, events=events
            )
            rows = [{"trial_id": trial_id, "attempt": attempt, **e} for e in events]
            await session.execute(
                pg_insert(TrialEventModel).values(rows).on_conflict_do_nothing()
            )
            flushed = len(rows)
        if checkpoint_values is not None:
            result = await session.execute(
                update(TrialModel)
                .where(TrialModel.id == trial_id, TrialModel.finished_at.is_(None))
                .values(**checkpoint_values)
            )
            if getattr(result, "rowcount", None) == 0:
                return PersistResult(flushed=flushed, trial_finished=True)
            checkpoint_written = True
    if not checkpoint_written or org_id is None:
        return PersistResult(flushed=flushed, checkpoint_written=checkpoint_written)

    from oddish.core.quota_enforcement import enforce_trial_quotas

    pause_requests: list[bool] = []
    cancelled = await enforce_trial_quotas(
        org_id=org_id,
        billed_user_id=billed_user_id,
        caller_trial_id=trial_id,
        quota_pause_callback=pause_requests.append,
    )
    return PersistResult(
        flushed=flushed,
        checkpoint_written=True,
        quota_checked=cancelled is not None,
        pause_requested=pause_requests[-1] if pause_requests else None,
    )


async def purge_events(trial_id: str, attempt: int | None) -> None:
    if attempt is None:
        return
    try:
        async with get_session() as session:
            await session.execute(
                delete(TrialEventModel).where(
                    TrialEventModel.trial_id == trial_id,
                    TrialEventModel.attempt <= attempt,
                )
            )
    except Exception as exc:
        console.print(f"[dim]Trial {trial_id} event purge failed: {exc}[/dim]")
