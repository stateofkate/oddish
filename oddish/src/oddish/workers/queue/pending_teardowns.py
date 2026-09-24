"""Stop again, on later reconciler sweeps, a monitor sandbox whose teardown
after a reap failed. The reaped job is not kept: only its sandbox handle waits,
in a queue_runtime_status row that only the reconciler writes."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from pydantic import BaseModel

from oddish.workers.console import console
from oddish.workers.launcher.providers.base import get_starter
from oddish.workers.queue.runtime_status import (
    get_queue_runtime_status,
    record_queue_runtime_status,
)

PENDING_TEARDOWNS = "monitor_pending_teardowns"  # the row's component
TEARDOWN_TRIES = 20
TEARDOWN_TIMEOUT_SECONDS = 60.0


class TeardownRetry(StrEnum):
    """How one retried stop ended: the metric line's outcome."""

    STOPPED = "stopped"
    FAILED = "failed"  # tried again next sweep
    GAVE_UP = "gave_up"  # the provider's own timeout is the backstop


class PendingTeardown(BaseModel):
    """A sandbox still to stop, and the stops tried since the reap's own."""

    provider: str
    external_id: str
    tries: int = 0


class PendingTeardowns(BaseModel):
    """The row's payload."""

    sandboxes: list[PendingTeardown] = []


async def retry_failed_teardowns(failed: list[tuple[str, str]]) -> None:
    """Stop each pending sandbox once more, until ``stop`` returns (stopped,
    or the provider says it is gone) or ``TEARDOWN_TRIES`` pass, then add the
    ``(provider, external_id)`` whose teardown failed in this sweep."""
    stored = PendingTeardowns.model_validate(
        await get_queue_runtime_status(PENDING_TEARDOWNS)
    )
    if not stored.sandboxes and not failed:
        return

    async def _stop(sandbox: PendingTeardown) -> None:
        stop = get_starter(sandbox.provider).stop(sandbox.external_id)
        await asyncio.wait_for(stop, timeout=TEARDOWN_TIMEOUT_SECONDS)

    results = await asyncio.gather(
        *map(_stop, stored.sandboxes), return_exceptions=True
    )
    pending: dict[tuple[str, str], PendingTeardown] = {}
    for sandbox, error in zip(stored.sandboxes, results, strict=True):
        sandbox.tries += 1
        if error is None:
            outcome = TeardownRetry.STOPPED
        elif sandbox.tries < TEARDOWN_TRIES:
            outcome = TeardownRetry.FAILED
            pending[sandbox.provider, sandbox.external_id] = sandbox
        else:
            outcome = TeardownRetry.GAVE_UP
        console.out(
            f"metric=monitor_teardown_retry outcome={outcome} "
            f"provider={sandbox.provider} external_id={sandbox.external_id} "
            f"tries={sandbox.tries}"
            + (f" error_type={type(error).__name__}" if error else ""),
            highlight=False,
        )
    for provider, external_id in failed:
        new = PendingTeardown(provider=provider, external_id=external_id)
        pending.setdefault((provider, external_id), new)
    await record_queue_runtime_status(
        PENDING_TEARDOWNS,
        PendingTeardowns(sandboxes=list(pending.values())).model_dump(),
    )
