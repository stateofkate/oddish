"""Durable post-commit sandbox teardown for the singleton reconciler.

Reaping stores handles in the same transaction that detaches them from jobs.
Cancellation during provider calls leaves that intent available to the next pass.
The existing monitor_pending_teardowns component also accepts worker handles.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import NamedTuple
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.helpers import cancel_job_by_worker
from oddish.db import get_session

from oddish.workers.console import console
from oddish.workers.launcher.providers.base import get_starter
from oddish.workers.queue.runtime_status import (
    get_queue_runtime_status,
)

PENDING_TEARDOWNS = "monitor_pending_teardowns"  # the row's component
TEARDOWN_TRIES = 20
TEARDOWN_BATCH_SIZE = 50
TEARDOWN_TIMEOUT_SECONDS = 60.0


class TeardownRetry(StrEnum):
    """How one retried stop ended: the metric line's outcome."""

    STOPPED = "stopped"
    FAILED = "failed"  # tried again next sweep
    GAVE_UP = "gave_up"  # the provider's own timeout is the backstop


class TeardownResult(NamedTuple):
    stopped: int
    errors: int


class PendingTeardown(BaseModel):
    """A sandbox still to stop, and the stops tried since the reap's own."""

    provider: str
    external_id: str
    tries: int = 0
    monitor_driven: bool = True


class PendingTeardowns(BaseModel):
    """The row's payload."""

    sandboxes: list[PendingTeardown] = []


@asynccontextmanager
async def _pending_for_update(
    session: AsyncSession,
) -> AsyncIterator[dict[tuple[str, str], PendingTeardown]]:
    """Lock and merge the existing record; provider calls never hold this lock."""
    await session.execute(
        text("""
        INSERT INTO queue_runtime_status (component, updated_at, payload)
        VALUES (:component, NOW(), '{"sandboxes": []}'::jsonb)
        ON CONFLICT (component) DO NOTHING
    """),
        {"component": PENDING_TEARDOWNS},
    )
    raw = await session.scalar(
        text("""
        SELECT payload FROM queue_runtime_status WHERE component = :component FOR UPDATE
    """),
        {"component": PENDING_TEARDOWNS},
    )
    stored = PendingTeardowns.model_validate(raw)
    pending = {(item.provider, item.external_id): item for item in stored.sandboxes}
    yield pending
    await session.execute(
        text("""
        UPDATE queue_runtime_status SET payload = CAST(:payload AS JSONB), updated_at = NOW()
        WHERE component = :component
    """),
        {
            "component": PENDING_TEARDOWNS,
            "payload": PendingTeardowns(
                sandboxes=list(pending.values())
            ).model_dump_json(),
        },
    )


async def remember_pending_teardowns(
    session: AsyncSession,
    targets: set[tuple[str, str]],
    *,
    monitor_handles: set[tuple[str, str]],
) -> None:
    """Commit teardown intent with the reap, before its old handles are cleared."""
    if not targets:
        return
    async with _pending_for_update(session) as pending:
        for provider, external_id in sorted(targets):
            pending.setdefault(
                (provider, external_id),
                PendingTeardown(
                    provider=provider,
                    external_id=external_id,
                    monitor_driven=(provider, external_id) in monitor_handles,
                ),
            )


async def retry_pending_teardowns() -> TeardownResult:
    """Retry durable handles once; cancellation leaves them available next sweep."""
    stored = PendingTeardowns.model_validate(
        await get_queue_runtime_status(PENDING_TEARDOWNS)
    )
    if not stored.sandboxes:
        return TeardownResult(0, 0)

    async def _stop(sandbox: PendingTeardown) -> None:
        async with asyncio.timeout(TEARDOWN_TIMEOUT_SECONDS):
            if sandbox.monitor_driven:
                await get_starter(sandbox.provider).stop(sandbox.external_id)
            elif not await cancel_job_by_worker(sandbox.provider, sandbox.external_id):
                raise RuntimeError("sandbox teardown was not confirmed")

    attempted = stored.sandboxes[:TEARDOWN_BATCH_SIZE]
    results = await asyncio.gather(*map(_stop, attempted), return_exceptions=True)
    updates: dict[tuple[str, str], PendingTeardown | None] = {}
    observed_tries = {(s.provider, s.external_id): s.tries for s in attempted}
    stopped = 0
    for sandbox, error in zip(attempted, results, strict=True):
        sandbox.tries += 1
        updates[sandbox.provider, sandbox.external_id] = None
        if error is None:
            outcome = TeardownRetry.STOPPED
            stopped += 1
        elif sandbox.tries < TEARDOWN_TRIES:
            outcome = TeardownRetry.FAILED
            updates[sandbox.provider, sandbox.external_id] = sandbox
        else:
            outcome = TeardownRetry.GAVE_UP
        console.out(
            f"metric=monitor_teardown_retry outcome={outcome} "
            f"provider={sandbox.provider} external_id={sandbox.external_id} "
            f"tries={sandbox.tries}"
            + (f" error_type={type(error).__name__}" if error else ""),
            highlight=False,
        )
    async with get_session() as session, _pending_for_update(session) as pending:
        for key, replacement in updates.items():
            current = pending.get(key)
            if current is None or current.tries != observed_tries[key]:
                continue  # Another pass already advanced this handle.
            del pending[key]
            if replacement is not None:
                pending[key] = (
                    replacement  # Failed stops rotate behind untried handles.
                )
    return TeardownResult(stopped, len(results) - stopped)
