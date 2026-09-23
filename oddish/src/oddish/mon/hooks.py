"""Harbor lifecycle events: report each one, then do what needs the environment."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from harbor.trial.hooks import TrialEvent

from oddish.mon import loops, memstat
from oddish.mon.client import (
    AttemptGone,
    ControlPlaneRefused,
    MonitorClient,
    MonitorRejected,
)
from oddish.mon.spec import AttemptSpec, summarize_hook_event
from oddish.mon.status import write_status
from oddish.workers.harbor import live_tail

logger = logging.getLogger(__name__)

_TAIL_STOPS = (TrialEvent.AGENT_END, TrialEvent.END, TrialEvent.CANCEL)


class LifecycleReporter:
    """Reports Harbor's stage events in order. One that cannot be delivered is
    kept and re-sent before anything newer, so an API outage never leaves a
    trial showing a stage it has already left. Events are telemetry: one our
    API refuses (4xx) is dropped. Its 410/401 ends the attempt: ``stopped``
    then raises, as the control loop does, so the driver is stopped."""

    def __init__(self, client: MonitorClient) -> None:
        self._client = client
        self._pending: list[dict[str, Any]] = []
        self._verdict: Exception | None = None
        self._ended = asyncio.Event()

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def report(self, summary: dict[str, Any]) -> None:
        self._pending.append(summary)
        await self.flush()

    async def flush(self) -> None:
        """Send what is pending, oldest first. Raises on an outage (the event
        stays pending) and on our API's 410/401; drops a refused event."""
        while self._pending:
            try:
                await self._client.lifecycle(self._pending[0])
            except (AttemptGone, MonitorRejected) as exc:
                self._verdict = exc
                self._ended.set()
                raise
            except ControlPlaneRefused as exc:
                # The client logged the route, status and body.
                logger.error(
                    "lifecycle event %s dropped: %s", self._pending[0].get("event"), exc
                )
            self._pending.pop(0)

    async def stopped(self) -> None:
        """Wait until a lifecycle post got our API's 410/401, then raise StopTrial."""
        await self._ended.wait()
        raise loops.StopTrial(str(self._verdict)) from self._verdict


def make_hook_callback(
    client: MonitorClient, spec: AttemptSpec, reporter: LifecycleReporter | None = None
):
    reporter = reporter or LifecycleReporter(client)

    async def on_event(hook_event: Any) -> None:
        event = hook_event.event
        write_status(trial_id=spec.trial_id, phase=event.value)
        logger.info("harbor event: %s", event.value)
        # Sizing evidence at every phase boundary: the sandbox's own cgroup and
        # the limit Docker gave each task container.
        logger.info("%s at %s", memstat.snapshot(), event.value)
        try:
            await reporter.report(summarize_hook_event(hook_event))
        except (AttemptGone, MonitorRejected):
            raise
        except Exception as exc:  # the trial outlives a control-plane blip
            logger.warning(
                "lifecycle event %s not delivered (%d waiting): %s",
                event.value,
                reporter.pending,
                exc,
            )

        environment = getattr(hook_event, "environment", None)
        if event == TrialEvent.AGENT_START and environment is not None:
            live_tail.start(
                trial_id=spec.trial_id,
                environment=environment,
                attempt=spec.trial_attempt,
                agent=spec.agent,
                model=spec.model,
                org_id=spec.org_id,
                billed_user_id=spec.billed_user_id,
            )
        elif event in _TAIL_STOPS:
            live_tail.request_stop(spec.trial_id)

    return on_event
