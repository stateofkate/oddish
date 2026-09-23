"""The driver's two database touchpoints, answered by the control plane.

The driver (``run_harbor_trial_async``) is used whole and unchanged. Its live
tail and quota loop take their persist / refresh / record callbacks as
arguments; ``build`` makes the monitor's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from oddish.mon.client import (
    AttemptGone,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    MonitorClient,
    MonitorRejected,
)
from oddish.mon.contract import MAX_EVENTS_PER_POST
from oddish.workers.harbor import live_tail, quota_control

logger = logging.getLogger(__name__)


class ControlState:
    """Latest answer from the control route, shared between loops; and the
    verdict (our API's 410/401) a call outside the loops got. The persist
    tick runs inside the driver's tailer, which swallows what it raises, so
    the verdict is recorded here for the runner's watcher to act on."""

    def __init__(self) -> None:
        self.pause_requested = False
        self.verdict: Exception | None = None
        self.ended = asyncio.Event()

    def end(self, verdict: Exception) -> None:
        self.verdict = verdict
        self.ended.set()


@dataclass(frozen=True)
class DriverSeams:
    persist: live_tail.PersistTick
    quota: quota_control.QuotaSeams


def build(client: MonitorClient, state: ControlState) -> DriverSeams:
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
        body: dict[str, Any] | None = None
        for index, batch in enumerate(batches):
            last = index == len(batches) - 1
            try:
                body = await client.events(
                    trial_attempt=attempt,
                    events=batch,
                    checkpoint=checkpoint_values if last else None,
                )
            except (AttemptGone, MonitorRejected) as exc:
                # The client recorded "stopped". The tailer swallows this, so
                # the runner's watcher is told, and it stops the driver.
                state.end(exc)
                raise
            except ControlPlaneRefused as exc:
                # Telemetry our API refuses (the client logged the status and
                # body) is dropped, as a refused lifecycle event is: retrying
                # the same batch would block the live tail for good.
                logger.error("live events: %d dropped: %s", len(batch), exc)
                sent += len(batch)
                continue
            except ControlPlaneUnavailable:
                if not sent:
                    raise
                logger.warning(
                    "live events: %d sent, %d left for the next tick",
                    sent,
                    len(events) - sent,
                )
                return live_tail.PersistResult(flushed=sent)
            sent += body["flushed"]
        if body is None:  # every batch was refused
            return live_tail.PersistResult(flushed=sent)
        return live_tail.PersistResult(**{**body, "flushed": sent})

    async def refresh_requested(org_id: str | None, billed_user_id: str | None) -> bool:
        return state.pause_requested

    async def record_state(trial_id: str, paused: bool) -> None:
        # Only reachable on a backend that can pause (Archil). The monitor
        # drives local Docker, which cannot, so the quota loop exits first.
        logger.warning(
            "quota pause state not recorded: trial_id=%s paused=%s", trial_id, paused
        )

    return DriverSeams(
        persist=persist,
        quota=quota_control.QuotaSeams(
            refresh_requested=refresh_requested, record_state=record_state
        ),
    )
