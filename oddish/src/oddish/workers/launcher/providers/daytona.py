"""Daytona status and cleanup for existing sandboxes; launches are disabled."""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager

from oddish.mon.contract import WarmRequest
from oddish.config import (
    DAYTONA_DISABLED_MESSAGE,
    DisabledEnvironmentError,
)
from oddish.workers.launcher.providers.base import (
    CodeBundle,
    MonitorSandboxError,
    SandboxProvider,
    SandboxSize,
    SandboxState,
    StatusReply,
    WarmUpObserver,
)

logger = logging.getLogger(__name__)

PYTHON = "/opt/oddish/.venv/bin/python"
_RUNNING_STATES = {"started", "starting"}
# States a sandbox does not come back from by itself. Any other state
# (creating, restoring, unknown, ...) is not read as an end.
_ENDED_STATES = {
    "stopping", "stopped", "destroying", "destroyed", "error", "build_failed",
    "archiving", "archived",
}  # fmt: skip


def _state_of(sandbox) -> str:
    return str(getattr(sandbox, "state", "") or "").lower().split(".")[-1]


@asynccontextmanager
async def _client():
    from daytona import AsyncDaytona

    client = AsyncDaytona()
    try:
        yield client
    finally:
        await client.close()


def _not_found_errors() -> tuple[type[Exception], ...]:
    """What the SDK raises for a sandbox that does not exist, as the existing
    Daytona reaper (``runtime/backends/daytona.py``) catches it."""
    from daytona.common.errors import DaytonaNotFoundError
    from daytona_api_client_async.exceptions import NotFoundException

    return (DaytonaNotFoundError, NotFoundException)


SNAPSHOT_PREFIX = "snapshot:"  # how a cached image names a Daytona snapshot


def _whole_units(size: SandboxSize) -> tuple[int, int]:
    """Daytona sizes in whole vCPUs and GiB. The headroom stops at Daytona's
    per-sandbox maximum ("Memory request 17GB exceeds maximum allowed per
    sandbox (16GB)"); a task that itself asks for more than the maximum is
    passed through as it is today, and Daytona answers."""
    from oddish.config import settings

    task_gb = math.ceil(size.memory_mb / 1024)
    with_headroom_gb = math.ceil(size.total_memory_mb / 1024)
    cap_gb = max(task_gb, settings.monitor_daytona_max_memory_gb)
    return math.ceil(size.cpu), min(with_headroom_gb, cap_gb)


class DaytonaStarter:
    provider = SandboxProvider.DAYTONA
    # Retain existing snapshot lookup/deletion, but never create another.
    warms_images = False

    async def start(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        size: SandboxSize | None = None,
        bundle: CodeBundle | None = None,
    ) -> str:
        raise DisabledEnvironmentError(DAYTONA_DISABLED_MESSAGE)

    def provisioned(self, size: SandboxSize) -> SandboxSize:
        cpu, memory_gb = _whole_units(size)
        return size.model_copy(
            update={"cpu": float(cpu), "memory_mb": memory_gb * 1024, "headroom_mb": 0}
        )

    async def build_warm_image(
        self,
        *,
        image: str,
        request: WarmRequest,
        size: SandboxSize | None = None,
        observer: WarmUpObserver | None = None,
    ) -> str | None:
        raise DisabledEnvironmentError(DAYTONA_DISABLED_MESSAGE)

    async def delete_warm_image(self, image: str) -> None:
        """Delete the snapshot behind a cached ``snapshot:<name>``; Daytona keeps
        snapshots until told otherwise, so the cache deletes what it drops."""
        if not image.startswith(SNAPSHOT_PREFIX):
            return
        name = image.removeprefix(SNAPSHOT_PREFIX)
        async with _client() as client:
            try:
                snapshot = await client.snapshot.get(name)
            except _not_found_errors():
                logger.info("daytona snapshot %s is already gone", name)
                return
            await client.snapshot.delete(snapshot)

    async def warm_image_exists(self, image: str) -> bool | None:
        if not image.startswith(SNAPSHOT_PREFIX):
            return None
        name = image.removeprefix(SNAPSHOT_PREFIX)
        try:
            async with _client() as client:
                await client.snapshot.get(name)
        except _not_found_errors():
            return False
        except Exception as exc:  # noqa: BLE001 -- the answer is "cannot say"
            logger.warning("daytona snapshot %s: existence unknown: %s", name, exc)
            return None
        return True

    async def stop(self, external_id: str) -> None:
        async with _client() as client:
            try:
                sandbox = await client.get(external_id)
            except _not_found_errors():
                logger.info("daytona sandbox %s is already gone", external_id)
                return
            # Anything else (an outage, a bad key) raises: the caller must not
            # believe a sandbox is gone when nobody could look.
            await client.delete(sandbox)

    async def status(self, external_id: str) -> StatusReply:
        # Every failure to ask (the client, the lookup, the exec) is one
        # error, as on Modal: the reaper reads it as no verdict, never as gone.
        try:
            async with _client() as client:
                try:
                    sandbox = await client.get(external_id)
                except _not_found_errors():
                    return StatusReply.gone()
                if _state_of(sandbox) not in _RUNNING_STATES:
                    return StatusReply.gone()
                result = await sandbox.process.exec(f"{PYTHON} -m oddish.mon status")
        except Exception as exc:
            raise MonitorSandboxError(f"daytona unreachable: {exc}") from exc
        return StatusReply(
            exit_code=int(result.exit_code), output=str(result.result or "")
        )

    async def sandbox_state(self, external_id: str) -> SandboxState:
        try:
            async with _client() as client:
                try:
                    sandbox = await client.get(external_id)
                except _not_found_errors():
                    return SandboxState(gone=True, detail="not found")
        except Exception as exc:
            raise MonitorSandboxError(f"daytona unreachable: {exc}") from exc
        state = _state_of(sandbox)
        reason = getattr(sandbox, "error_reason", None)
        detail = f"state {state}" + (f": {reason}" if reason else "")
        return SandboxState(gone=state in _ENDED_STATES, detail=detail)
