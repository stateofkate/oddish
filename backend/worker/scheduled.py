"""Bound scheduled queue work before Modal's hard timeout, then report its outcome."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from oddish.db import close_database_connections
from oddish.workers.queue.runtime_status import record_queue_runtime_status

logger = logging.getLogger(__name__)

# Reserve the 30s DB command bound, 5s lease release, 3s status write, and 5s close.
FINALIZE_RESERVE_SECONDS = 45.0
FINALIZE_TIMEOUT_SECONDS = 5.0


@dataclass
class QueueCycle:
    payload: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    state: Literal["completed", "deferred", "failed", "cancelled"] = "completed"

    @asynccontextmanager
    async def phase(self, name: str, *, seconds: float) -> AsyncIterator[None]:
        """A failed maintenance phase must not starve unrelated maintenance."""
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                yield
        except TimeoutError:
            if not deadline.expired():
                self.errors.append(f"{name}: dependency timed out")
                logger.exception("Queue phase failed: %s", name)
            else:
                self.deferred.append(name)
                logger.warning("Queue phase deferred after %.1fs: %s", seconds, name)
        except Exception as exc:
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.exception("Queue phase failed: %s", name)


async def run_queue_cycle(
    component: str,
    operation: Callable[[QueueCycle], Awaitable[None]],
    *,
    timeout_seconds: float,
) -> None:
    cycle = QueueCycle()
    started = asyncio.get_running_loop().time()
    deadline = asyncio.timeout(max(0, timeout_seconds - FINALIZE_RESERVE_SECONDS))
    try:
        async with deadline:
            await operation(cycle)
    except TimeoutError:
        if not deadline.expired():
            cycle.state = "failed"
            cycle.errors.append("dependency timed out")
            raise
        cycle.deferred.append("cycle deadline")
    except asyncio.CancelledError:
        cycle.state = "cancelled"
        raise
    except Exception as exc:
        cycle.state = "failed"
        cycle.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if cycle.state == "completed":
            cycle.state = (
                "failed"
                if cycle.errors
                else "deferred"
                if cycle.deferred
                else "completed"
            )
        cycle.payload.update(
            state=cycle.state,
            ok=cycle.state == "completed",
            errors=cycle.errors,
            deferred_phases=cycle.deferred,
            duration_seconds=round(asyncio.get_running_loop().time() - started, 2),
        )
        # The writer has its own short timeout; a failed write leaves the previous
        # heartbeat stale. Never turn cancelled/partial work into a success stamp.
        await record_queue_runtime_status(component, cycle.payload)
        try:
            async with asyncio.timeout(FINALIZE_TIMEOUT_SECONDS):
                await close_database_connections()
        except Exception:
            logger.exception("Queue connection cleanup failed: %s", component)
        logger.info("Queue cycle ended: component=%s state=%s", component, cycle.state)
