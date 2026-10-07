"""Durable setup event ingestion and org-scoped reads."""

import logging
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints._common import get_trial_for_org_core
from oddish.db.models import TrialModel, TrialProgressEventModel
from oddish.progress import ProgressEvent, ProgressHistory, SetupPayload

logger = logging.getLogger(__name__)

_PAYLOAD = TypeAdapter(SetupPayload)
HISTORY_LIMIT = 5000


async def retain_setup_events(
    session: AsyncSession, *, trial_id: str, attempt: int, events: list[dict[str, Any]]
) -> None:
    rows = []
    for event in events:
        if event["kind"] not in ("env_subphase", "env_info"):
            continue
        try:
            payload = _PAYLOAD.validate_python(
                {**event["payload"], "kind": event["kind"]}
            )
            seq = event["seq"]
            if attempt < 1 or not isinstance(seq, int) or seq < 0:
                raise ValueError("Invalid setup progress identity")
        except (ValidationError, ValueError, TypeError):
            # A malformed optional setup fact must not drop the live transcript.
            # Never log its payload, which has not passed the retention allowlist.
            logger.warning(
                "Trial %s has invalid setup history for attempt %s", trial_id, attempt
            )
            continue
        rows.append(
            {
                "trial_id": trial_id,
                "attempt": attempt,
                "event_key": f"live:{seq}",
                "kind": payload.kind.value,
                "source_seq": seq,
                "schema_version": getattr(payload, "schema_version", 1),
                "payload": payload.model_dump(mode="json"),
            }
        )
    if rows:
        current_attempt = await session.scalar(
            select(TrialModel.attempts)
            .where(TrialModel.id == trial_id)
            .with_for_update()
        )
        if attempt != current_attempt:
            logger.warning(
                "Ignoring stale or future setup history for trial %s attempt %s",
                trial_id,
                attempt,
            )
            return
        await session.execute(
            pg_insert(TrialProgressEventModel).values(rows).on_conflict_do_nothing()
        )


async def read_progress_history(
    session: AsyncSession, *, trial_id: str, org_id: str | None = None
) -> ProgressHistory:
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    rows = (
        await session.scalars(
            select(TrialProgressEventModel)
            .where(TrialProgressEventModel.trial_id == trial.id)
            .order_by(
                TrialProgressEventModel.attempt.desc(),
                TrialProgressEventModel.source_seq,
            )
            .limit(HISTORY_LIMIT + 1)
        )
    ).all()
    events = [
        ProgressEvent(
            attempt=row.attempt,
            event_key=row.event_key,
            kind=row.kind,
            source_seq=row.source_seq,
            schema_version=row.schema_version,
            recorded_at=row.recorded_at,
            payload=row.payload,
        )
        for row in rows[:HISTORY_LIMIT]
    ]
    return ProgressHistory(
        trial_id=trial.id,
        current_attempt=trial.attempts,
        history_available=bool(events),
        history_truncated=len(rows) > HISTORY_LIMIT
        or any(e.source_seq == HISTORY_LIMIT for e in events),
        events=events,
    )
