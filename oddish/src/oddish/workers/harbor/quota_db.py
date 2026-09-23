"""The database side of quota pause control, for the worker path.

``quota_control`` takes its two touchpoints as a ``QuotaSeams`` callback pair.
The worker passes ``DATABASE_SEAMS``; the in-sandbox monitor passes seams that
answer from the control plane.
"""

from __future__ import annotations

from oddish.core.quota_pause import quota_pause_requested
from oddish.db import TrialModel, TrialStatus, get_session
from oddish.workers.harbor.quota_control import QuotaSeams


async def record_pause_state(trial_id: str, paused: bool) -> None:
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id, with_for_update=True)
        if trial is None:
            return
        if paused and trial.status == TrialStatus.RUNNING:
            trial.status = TrialStatus.PAUSED
        elif not paused and trial.status == TrialStatus.PAUSED:
            trial.status = TrialStatus.RUNNING


async def refresh_pause_requested(
    org_id: str | None, billed_user_id: str | None
) -> bool:
    async with get_session() as session:
        return await quota_pause_requested(
            session,
            org_id=org_id,
            billed_user_id=billed_user_id,
        )


DATABASE_SEAMS = QuotaSeams(
    refresh_requested=refresh_pause_requested, record_state=record_pause_state
)
