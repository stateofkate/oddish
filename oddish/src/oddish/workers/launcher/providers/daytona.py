"""Daytona: the monitor runs inside a Daytona sandbox that has Docker in it.

Credentials are the ones Harbor's Daytona environment already uses
(``DAYTONA_API_KEY``, optionally ``DAYTONA_API_URL`` / ``DAYTONA_TARGET``), read
from the process environment by the SDK. Nothing new is stored.

The monitor image has no ENTRYPOINT: the sandbox boots idle and ``start`` runs
the monitor exactly once. Warm filesystem snapshots preserve Docker images;
the entrypoint starts Docker with fresh process runtime state on every boot.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from oddish.mon.contract import NOTHING_TO_WARM, WARM_VAR, WarmRequest
from oddish.workers.launcher.providers.base import (
    WARM_UP_SECONDS,
    MonitorSandboxError,
    SandboxProvider,
    SandboxSize,
    StatusReply,
    warm_up_lifetime_seconds,
)

logger = logging.getLogger(__name__)

ENTRYPOINT = "/usr/local/bin/mon-entrypoint.sh"
PYTHON = "/opt/oddish/.venv/bin/python"
_RUNNING_STATES = {"started", "starting"}


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


def _image(image: str):
    from daytona import Image

    if image.endswith("Dockerfile"):
        return Image.from_dockerfile(str(Path(image).resolve()))
    return Image.base(image)


def _not_found_errors() -> tuple[type[Exception], ...]:
    """What the SDK raises for a sandbox that does not exist, as the existing
    Daytona reaper (``runtime/backends/daytona.py``) catches it."""
    from daytona.common.errors import DaytonaNotFoundError
    from daytona_api_client_async.exceptions import NotFoundException

    return (DaytonaNotFoundError, NotFoundException)


SNAPSHOT_PREFIX = "snapshot:"  # how a cached image names a Daytona snapshot
SNAPSHOT_NAME_PREFIX = "oddish-mon-warm-"


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


def _create_params(
    *,
    name: str,
    image: str,
    env: dict[str, str],
    size: SandboxSize | None,
    lifetime_seconds: int | None = None,
):
    from daytona import (
        CreateSandboxFromImageParams,
        CreateSandboxFromSnapshotParams,
        Resources,
    )

    from oddish.config import settings

    lifetime = lifetime_seconds or settings.monitor_attempt_deadline_seconds
    common = {
        "name": name,
        # The provider's creation slot: the token, the model keys and the
        # registry login never touch our API or the database.
        "env_vars": env,
        # The labels the existing Daytona reaper looks for: past
        # ``oddish.expires_at`` it deletes the sandbox, which is the hard
        # runtime bound (auto-stop only counts inactivity).
        "labels": {
            "oddish.monitor": "true",
            "harbor.managed": "true",
            "oddish.managed": "true",
            "oddish.expires_at": str(int(time.time()) + lifetime),
        },
        # An untracked sandbox (a crash between create and saving its handle)
        # stops once nobody has talked to it for its lifetime.
        "auto_stop_interval": lifetime // 60,
        "auto_delete_interval": 0,
        "network_block_all": False,  # Docker inside needs the network
    }
    if image.startswith(SNAPSHOT_PREFIX):
        # No resources here: a snapshot boots at the size it was taken at.
        return CreateSandboxFromSnapshotParams(
            snapshot=image.removeprefix(SNAPSHOT_PREFIX), **common
        )
    cpu, memory = (
        _whole_units(size)
        if size
        # The settings only cover a task that pins nothing.
        else (settings.monitor_daytona_cpu, settings.monitor_daytona_memory_gb)
    )
    return CreateSandboxFromImageParams(
        image=_image(image),
        resources=Resources(
            cpu=cpu,
            memory=memory,
            disk=settings.monitor_daytona_disk_gb,
            **(
                {"gpu": size.gpu_count, "gpu_type": size.gpu_type}
                if size and size.gpu_count
                else {}
            ),
        ),
        **common,
    )


async def _start_monitor_once(sandbox) -> None:
    """The image has no ENTRYPOINT, so a sandbox boots idle whether it comes
    from the image or from a snapshot, and this is the only place the monitor
    is started."""
    await sandbox.process.exec(
        f"nohup {ENTRYPOINT} > /var/log/oddish-mon-entry.log 2>&1 &", timeout=30
    )


def _check_size(sandbox, image: str, size: SandboxSize | None) -> None:
    """A snapshot boots at its own size. If that is not the task's size the
    launcher drops the cache entry and starts cold, so a task is never run in a
    sandbox smaller than it asked for."""
    if size is None or not image.startswith(SNAPSHOT_PREFIX):
        return
    gpu_count = int(getattr(sandbox, "gpu", 0) or 0)
    # A task that names no GPU type takes whichever type it was given.
    typed = bool(size.gpu_count and size.gpu_type)
    gpu_type = getattr(sandbox, "gpu_type", None) if typed else None
    got = (
        getattr(sandbox, "cpu", None),
        getattr(sandbox, "memory", None),
        gpu_count,
        str(getattr(gpu_type, "value", gpu_type)).upper() if gpu_type else None,
    )
    want = (
        *_whole_units(size),
        size.gpu_count,
        size.gpu_type.upper() if typed and size.gpu_type else None,
    )
    if got != want:
        raise MonitorSandboxError(f"snapshot sandbox is {got}, the task needs {want}")


class DaytonaStarter:
    provider = SandboxProvider.DAYTONA

    async def start(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        size: SandboxSize | None = None,
    ) -> str:
        from oddish.config import settings

        async with _client() as client:
            sandbox = await client.create(
                _create_params(name=name, image=image, env=env, size=size),
                timeout=settings.monitor_daytona_create_timeout_seconds,
            )
            try:
                _check_size(sandbox, image, size)
                await _start_monitor_once(sandbox)
            except Exception:
                await client.delete(sandbox)  # only the sandbox created above
                raise
            return sandbox.id

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
    ) -> str | None:
        """Pull the task's images in a sandbox that holds no secrets, snapshot
        its disk and return ``snapshot:<name>``. ``None``: nothing to pull.

        A sandbox booted from a snapshot cannot be given a size, so the warm-up
        runs at the task's size and ``start`` checks the size it got."""
        from oddish.config import settings

        snapshot = f"{SNAPSHOT_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        async with _client() as client:
            sandbox = await client.create(
                _create_params(
                    name=snapshot,
                    image=image,
                    env={WARM_VAR: request.model_dump_json()},
                    size=size,
                    lifetime_seconds=warm_up_lifetime_seconds(),
                ),
                timeout=settings.monitor_daytona_create_timeout_seconds,
            )
            try:
                pulled = await sandbox.process.exec(
                    f"{ENTRYPOINT} warm", timeout=WARM_UP_SECONDS
                )
                if int(pulled.exit_code) == NOTHING_TO_WARM:
                    return None
                if int(pulled.exit_code) != 0:
                    raise MonitorSandboxError(f"warm-up exited {pulled.exit_code}")
                # The warm entrypoint waits for Docker to shut down before
                # succeeding; the snapshot contains images, not live daemons.
                await sandbox._experimental_create_snapshot(
                    snapshot, timeout=WARM_UP_SECONDS
                )
                return f"{SNAPSHOT_PREFIX}{snapshot}"
            finally:
                await client.delete(sandbox)  # only the sandbox created above

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
        async with _client() as client:
            try:
                sandbox = await client.get(external_id)
            except _not_found_errors():
                return StatusReply.gone()
            except Exception as exc:
                raise MonitorSandboxError(f"daytona unreachable: {exc}") from exc
            if _state_of(sandbox) not in _RUNNING_STATES:
                return StatusReply.gone()
            result = await sandbox.process.exec(f"{PYTHON} -m oddish.mon status")
            return StatusReply(
                exit_code=int(result.exit_code), output=str(result.result or "")
            )
