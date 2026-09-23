"""Start and stop the sandbox whose entrypoint is the monitor.

One small interface, one file per provider. Secrets are passed only through the
provider's own creation slot. Nothing outside the provider files names a
provider: the launcher routes by the trial's environment, and the reconciler
and settlement look the starter up from the provider stored on the job row.

To add a provider: one file with a class that has ``provider``, ``start``,
``stop`` and ``status``, and one line in ``_STARTERS``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum
from importlib import import_module
from typing import Protocol

from pydantic import BaseModel

from oddish.mon.contract import WarmRequest


class MonitorSandboxError(RuntimeError):
    pass


class MonitorSandboxGone(MonitorSandboxError):
    """The provider says the sandbox has ended. Before the monitor's first
    check-in that is a failed start, not a slow one."""


class SandboxProvider(StrEnum):
    """Providers a monitor sandbox can run on. The value is the Harbor
    environment name and what ``worker_jobs.provider`` stores."""

    MODAL = "modal"
    DAYTONA = "daytona"


class SandboxSize(BaseModel, frozen=True):
    """What the task asked for. The monitor sandbox hosts the task's container,
    so it must be at least this big; an inner container cannot get memory or a
    GPU its outer sandbox lacks."""

    cpu: float
    memory_mb: int
    gpu_type: str | None = None
    gpu_count: int = 0
    # Memory for the monitor and Docker daemons that share the sandbox with
    # the task's container, on top of ``memory_mb``. Each provider folds it
    # into what it allocates (Daytona: up to its per-sandbox maximum).
    headroom_mb: int = 0

    @property
    def total_memory_mb(self) -> int:
        return self.memory_mb + self.headroom_mb

    @classmethod
    def default(cls) -> SandboxSize:
        """The floor for a task that pins nothing, the same on every provider."""
        from oddish.config import settings

        return cls(
            cpu=settings.monitor_daytona_cpu,
            memory_mb=settings.monitor_daytona_memory_gb * 1024,
        )

    @classmethod
    def from_span_resources(
        cls,
        resources: object | None,
        *,
        verifier: object | None = None,
        memory_headroom_mb: int = 0,
    ) -> SandboxSize | None:
        """``None`` when the task pins nothing: the provider file's default
        applies, as it did before. Limits win over requests, because the
        container may grow to its limit. ``verifier`` is a separate verifier's
        resources: it runs in this same sandbox, so CPU and memory are the
        larger of the two, from whichever side pins; the GPU stays the
        agent's. ``memory_headroom_mb`` is for the
        OUTER sandbox only, for the monitor and Docker daemons that share it;
        the task container's own size is untouched."""
        pinned = [
            r
            for r in (resources, verifier)
            if getattr(r, "spec_source", None) in ("pinned", "override")
        ]
        cpu = max(
            (max(r.cpu_request or 0, r.cpu_limit or 0) for r in pinned), default=0
        )
        memory = max(
            (max(r.mem_request_mb or 0, r.mem_limit_mb or 0) for r in pinned), default=0
        )
        gpu_count = getattr(resources, "gpu_count", 0) or 0
        if (cpu <= 0 or memory <= 0) and gpu_count > 0:
            # A GPU alone pins the task, as on the worker path. Size the rest
            # at the monitor's defaults so the GPU is still provisioned.
            from oddish.config import settings

            cpu = cpu if cpu > 0 else settings.monitor_daytona_cpu
            memory = memory if memory > 0 else settings.monitor_daytona_memory_gb * 1024
        if cpu <= 0 or memory <= 0:
            return None
        return cls(
            cpu=cpu,
            memory_mb=memory,
            headroom_mb=max(0, int(memory_headroom_mb)),
            gpu_type=getattr(resources, "gpu_type", None),
            gpu_count=gpu_count,
        )

    def billed_as(self, resources):
        """``resources`` with the outer sandbox's real CPU and memory, so the
        cost span prices what was provisioned, provider rounding and headroom
        included."""
        return replace(
            resources,
            cpu_request=self.cpu,
            cpu_limit=self.cpu,
            mem_request_mb=self.total_memory_mb,
            mem_limit_mb=self.total_memory_mb,
        )


class StatusReply(BaseModel, frozen=True):
    """The answer to ``oddish-mon status`` run inside the sandbox."""

    exit_code: int
    output: str = ""

    @property
    def alive(self) -> bool:
        return self.exit_code == 0

    @classmethod
    def gone(cls) -> StatusReply:
        """The sandbox has ended, so the monitor in it has too."""
        return cls(exit_code=1)


class MonitorSandboxStarter(Protocol):
    provider: SandboxProvider

    async def start(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        size: SandboxSize | None = None,
    ) -> str:
        """Create the sandbox and return the handle used for status and teardown.

        The image's entrypoint is the monitor. A provider starts it exactly
        once, by whatever means that provider runs an image's entrypoint
        (Modal: the sandbox command; Daytona: the image ENTRYPOINT)."""

    def provisioned(self, size: SandboxSize) -> SandboxSize:
        """What this provider really allocates for ``size`` (it may round up)."""

    async def stop(self, external_id: str) -> None: ...

    async def status(self, external_id: str) -> StatusReply:
        """Run ``oddish-mon status`` inside the sandbox."""


# The most each step of a warm-up (the pull, then the snapshot) may take.
WARM_UP_SECONDS = 1800

# How long the cache boots a warm image. The provider keeps it a day longer, so
# a cache hit never names an image the provider has already deleted.
WARM_IMAGE_READY_SECONDS = 7 * 24 * 3600
WARM_IMAGE_TTL_SECONDS = WARM_IMAGE_READY_SECONDS + 24 * 3600


def warm_up_lifetime_seconds() -> int:
    """The longest a live warm-up runs: a Daytona create, then the pull and the
    snapshot, each bounded; plus a margin. The warm-up sandbox lives this long,
    and a WARMING marker is taken for abandoned only after it."""
    from oddish.config import settings

    return settings.monitor_daytona_create_timeout_seconds + 2 * WARM_UP_SECONDS + 300


class WarmImageBuilder(Protocol):
    """Optional capability. A provider without it never uses the warm-image cache."""

    async def build_warm_image(
        self, *, image: str, request: WarmRequest, size: SandboxSize | None = None
    ) -> str | None:
        """Pull the task's images in a secret-free sandbox, snapshot its disk and
        return a reference ``start`` can boot from. ``None``: the task names no
        image to pull. Raises when the warm-up failed, so it is tried again."""


# Provider name (the Harbor environment name) -> "module:Class", imported on
# first use so a provider's SDK is only needed where that provider runs.
_STARTERS: dict[str, str | Callable[[], MonitorSandboxStarter]] = {
    SandboxProvider.MODAL: "oddish.workers.launcher.providers.modal:ModalStarter",
    SandboxProvider.DAYTONA: "oddish.workers.launcher.providers.daytona:DaytonaStarter",
}


def get_starter(name: SandboxProvider | str) -> MonitorSandboxStarter:
    try:
        entry = _STARTERS[name]
    except KeyError:
        raise MonitorSandboxError(
            f"No monitor sandbox starter named {name!r}"
        ) from None
    if callable(entry):
        return entry()
    module, _, attribute = entry.partition(":")
    return getattr(import_module(module), attribute)()


def provider_for_environment(environment: str | None) -> SandboxProvider | str | None:
    """Which starter drives a trial that asked for ``environment``.

    A trial runs on the provider it asked for. ``None`` means no starter exists
    for it yet, and the trial stays on the worker path. ``monitor_sandbox_starter``
    forces one provider for everything (self-hosted setups and tests).
    """
    from oddish.config import settings

    if settings.monitor_sandbox_starter:
        return settings.monitor_sandbox_starter
    name = (environment or settings.harbor_environment or "").strip().lower()
    try:
        return SandboxProvider(name)
    except ValueError:
        return None
