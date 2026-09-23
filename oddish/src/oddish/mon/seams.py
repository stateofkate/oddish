"""Point the driver's two database touchpoints at the control plane.

The driver (``run_harbor_trial_async``) is used whole and unchanged. Its live
tail and quota loop take process-wide overrides; these are those overrides.
"""

from __future__ import annotations

import logging
from typing import Any

from oddish.mon.client import (
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    MonitorClient,
)
from oddish.mon.spec import MAX_EVENTS_PER_POST
from oddish.workers.harbor import live_tail, quota_control

logger = logging.getLogger(__name__)


class ControlState:
    """Latest answer from the control route, shared between loops."""

    pause_requested: bool = False


def install(client: MonitorClient, state: ControlState) -> None:
    async def persist(
        *,
        attempt: int,
        events: list[dict[str, Any]],
        checkpoint_values: dict[str, Any] | None,
        **_: Any,
    ) -> live_tail.PersistResult:
        # One tick can hold more events than the route accepts: post them in
        # order, in batches it accepts, with the checkpoint on the last one.
        # If a batch fails after some were sent, report those as flushed and
        # the tailer keeps the rest pending for its next tick.
        batches = [
            events[start : start + MAX_EVENTS_PER_POST]
            for start in range(0, len(events), MAX_EVENTS_PER_POST)
        ] or [[]]
        sent = 0
        for index, batch in enumerate(batches):
            last = index == len(batches) - 1
            try:
                body = await client.events(
                    trial_attempt=attempt,
                    events=batch,
                    checkpoint=checkpoint_values if last else None,
                )
            except (ControlPlaneUnavailable, ControlPlaneRefused):
                if not sent:
                    raise
                logger.warning(
                    "live events: %d sent, %d left for the next tick",
                    sent,
                    len(events) - sent,
                )
                return live_tail.PersistResult(flushed=sent)
            sent += body["flushed"]
        return live_tail.PersistResult(**{**body, "flushed": sent})

    async def refresh_requested(org_id: str | None, billed_user_id: str | None) -> bool:
        return state.pause_requested

    async def record_state(trial_id: str, paused: bool) -> None:
        # Only reachable on a backend that can pause (Archil). The monitor
        # drives local Docker, which cannot, so the quota loop exits first.
        logger.warning(
            "quota pause state not recorded: trial_id=%s paused=%s", trial_id, paused
        )

    live_tail.set_default_persist(persist)
    quota_control.set_default_seams(refresh_requested, record_state)
