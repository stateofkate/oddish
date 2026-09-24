"""Modal: the monitor runs as the entrypoint of a VM-runtime sandbox."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from oddish.mon.contract import NOTHING_TO_WARM, WARM_VAR, WarmRequest
from oddish.workers.launcher.providers.base import (
    ENTRYPOINT,
    WARM_IMAGE_TTL_SECONDS,
    WARM_UP_SECONDS,
    CodeBundle,
    MonitorSandboxError,
    SandboxProvider,
    SandboxSize,
    SandboxState,
    StatusReply,
    monitor_start,
    warm_up_lifetime_seconds,
)

logger = logging.getLogger(__name__)

# Harbor's ``modal_exact_gpu_type`` names: Modal must not upgrade H100 to H200
# or A100 40GB to 80GB, as on the worker path.
_EXACT_GPU_TYPES = {"A100": "A100-40GB", "H100": "H100!"}


def _memory_mb(size: SandboxSize) -> int:
    """The VM's memory: the task's size and the headroom, plus the VM's own
    share when there is headroom. A Modal VM shows its guest less memory than
    it is given, and the guest kernel's share grows with the VM, so a flat
    headroom shrinks what is left for the task as the task grows (see
    ``monitor_modal_memory_headroom_percent``)."""
    from oddish.config import settings

    total = size.total_memory_mb
    if size.headroom_mb <= 0:
        return total
    percent = settings.monitor_modal_memory_headroom_percent
    return total + math.ceil(total * percent / 100)


def _modal_size(size: SandboxSize | None) -> dict:
    # A task that pins nothing gets the Daytona floor, not Modal's SDK default.
    size = size or SandboxSize.default()
    kwargs: dict = {"cpu": size.cpu, "memory": _memory_mb(size)}
    if size.gpu_count:
        gpu = size.gpu_type or "any"
        gpu = _EXACT_GPU_TYPES.get(gpu.strip().upper(), gpu)
        kwargs["gpu"] = f"{gpu}:{size.gpu_count}" if size.gpu_count > 1 else gpu
    return kwargs


class ModalStarter:
    """A Modal sandbox with Docker enabled.

    Secrets go in through ``Secret.from_dict`` at creation, the same slot Harbor
    uses for a sandbox's environment today. The sandbox timeout is the attempt
    deadline, so an untracked sandbox (a crash between create and saving its
    handle) is bounded by the provider.
    """

    provider = SandboxProvider.MODAL

    async def start(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        size: SandboxSize | None = None,
        bundle: CodeBundle | None = None,
    ) -> str:
        import modal

        from oddish.config import settings

        app = await modal.App.lookup.aio(
            settings.monitor_modal_app, create_if_missing=True
        )
        command, env = monitor_start(env, bundle)
        sandbox = await modal.Sandbox.create.aio(
            *command,
            app=app,
            image=self._image(image),
            secrets=[modal.Secret.from_dict(env)],
            timeout=settings.monitor_attempt_deadline_seconds,
            # A real VM, so the inner Docker daemon gets normal networking and
            # cgroups. (Harbor's gVisor mode needs host-network workarounds.)
            experimental_options={"vm_runtime": True},
            **_modal_size(size),
        )
        return sandbox.object_id

    @staticmethod
    def _image(image: str):
        import modal

        if image.startswith("im-"):  # a disk snapshot from build_warm_image
            return modal.Image.from_id(image)
        if image.endswith("Dockerfile"):
            # Build on Modal from the repo's Dockerfile: no registry needed.
            dockerfile = Path(image).resolve()
            return modal.Image.from_dockerfile(
                str(dockerfile), context_dir=str(dockerfile.parent)
            ).entrypoint([])
        return modal.Image.from_registry(image).entrypoint([])

    async def warm_image_exists(self, image: str) -> bool | None:
        import modal

        if not image.startswith("im-"):
            return None
        try:
            await modal.Image.from_id(image).hydrate.aio()
        except modal.exception.NotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001 -- the answer is "cannot say"
            logger.warning("modal image %s: existence unknown: %s", image, exc)
            return None
        return True

    def provisioned(self, size: SandboxSize) -> SandboxSize:
        # Modal allocates fractional CPU and MiB as asked.
        return size.model_copy(update={"memory_mb": _memory_mb(size), "headroom_mb": 0})

    async def build_warm_image(
        self, *, image: str, request: WarmRequest, size: SandboxSize | None = None
    ) -> str | None:
        """Pull the task's images in a sandbox that holds no secrets, snapshot
        its disk and return the snapshot id. ``None``: nothing to pull.
        A Modal snapshot boots at whatever size ``start`` asks; ``size`` only
        sizes the warm-up sandbox that pulls."""
        import modal

        from oddish.config import settings

        app = await modal.App.lookup.aio(
            settings.monitor_modal_app, create_if_missing=True
        )
        sandbox = await modal.Sandbox.create.aio(
            "sleep",
            str(warm_up_lifetime_seconds()),
            app=app,
            image=self._image(image),
            secrets=[modal.Secret.from_dict({WARM_VAR: request.model_dump_json()})],
            timeout=warm_up_lifetime_seconds(),
            experimental_options={"vm_runtime": True},
            **_modal_size(size),
        )
        try:
            process = await sandbox.exec.aio(ENTRYPOINT, "warm")
            code = await process.wait.aio()
            if code == NOTHING_TO_WARM:
                return None
            if code != 0:
                raise MonitorSandboxError(f"warm-up exited {code}")
            snapshot = await sandbox.snapshot_filesystem.aio(
                timeout=WARM_UP_SECONDS, ttl=WARM_IMAGE_TTL_SECONDS
            )
            return snapshot.object_id
        finally:
            await sandbox.terminate.aio()  # only the sandbox created above

    async def stop(self, external_id: str) -> None:
        import modal

        try:
            sandbox = await modal.Sandbox.from_id.aio(external_id)
            await sandbox.terminate.aio()
        except modal.exception.NotFoundError:
            return  # already gone, as Daytona's stop treats it

    async def status(self, external_id: str) -> StatusReply:
        import modal

        try:
            sandbox = await modal.Sandbox.from_id.aio(external_id)
            finished = await sandbox.poll.aio() is not None
            if finished:
                return StatusReply.gone()
            process = await sandbox.exec.aio("python", "-m", "oddish.mon", "status")
            output = await process.stdout.read.aio()
            exit_code = await process.wait.aio()
        except modal.exception.NotFoundError:
            return StatusReply.gone()
        except Exception as exc:
            # The provider could not be asked: no verdict, as on Daytona.
            raise MonitorSandboxError(f"modal unreachable: {exc}") from exc
        return StatusReply(exit_code=exit_code, output=output)

    async def sandbox_state(self, external_id: str) -> SandboxState:
        import modal

        try:
            sandbox = await modal.Sandbox.from_id.aio(external_id)
            code = await sandbox.poll.aio()
        except modal.exception.NotFoundError:
            return SandboxState(gone=True, detail="not found")
        except Exception as exc:
            raise MonitorSandboxError(f"modal unreachable: {exc}") from exc
        if code is None:
            return SandboxState(gone=False)
        return SandboxState(gone=True, detail=f"exit code {code}")
