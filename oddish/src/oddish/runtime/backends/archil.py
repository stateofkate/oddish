from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)


class ArchilBackend:
    name = "archil"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=False,
            memory_snapshot_fork=True,
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        return {"drain_on_pause": True, **base_kwargs}

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        try:
            from archil import Archil

            for target in _cleanup_targets():
                client = Archil(**target, timeout=120)
                try:
                    try:
                        sandbox = await client.sandboxes.get.aio(external_id)
                    except Exception as exc:
                        if getattr(exc, "status", None) == 404:
                            continue
                        raise
                    if sandbox.status not in {"stopped", "exited", "failed"}:
                        sandbox = await sandbox.stop.aio()
                    await sandbox.delete.aio()
                finally:
                    await client.close.aio()
                logger.info("ArchilBackend.teardown: terminated %s", external_id)
                return True
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                logger.info("ArchilBackend.teardown: %s already gone", external_id)
                return True
            logger.exception(
                "ArchilBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("ArchilBackend.teardown: %s already gone", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        yield None


def _cleanup_targets() -> list[dict[str, str]]:
    """Client settings for every region a trial's sandbox may be in.

    Harbor places each trial in a region from ``ARCHIL_REGIONS`` with that
    region's key. A test fleet's ``ARCHIL_BASE_URL`` is the one target, with
    the SDK resolving its ``ARCHIL_REGION`` and ``ARCHIL_API_KEY``.
    """
    if base_url := os.environ.get("ARCHIL_BASE_URL"):
        return [{"base_url": base_url}]
    from harbor.environments.archil import region_key_var, region_weights

    targets: list[dict[str, str]] = [
        {"region": region, "api_key": key}
        for region in region_weights()
        if (key := os.environ.get(region_key_var(region)))
    ]
    if not targets:
        raise ValueError("ARCHIL_REGIONS lists no region with an API key")
    return targets


_: ExecutionBackend = ArchilBackend()
