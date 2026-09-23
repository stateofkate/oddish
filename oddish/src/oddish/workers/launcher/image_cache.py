"""Reuse a task's pulled images across monitor sandboxes.

A fresh sandbox has an empty Docker cache, so every trial of a heavy task would
pull its images again. The first trial instead runs a warm-up sandbox that pulls
them and snapshots its disk; later trials boot from that snapshot.

Provider-neutral: the provider only implements ``build_warm_image``. Entries are
small JSON objects in the storage bucket. The cache can only make a launch
faster: every failure here falls back to the plain monitor image.
"""

from __future__ import annotations

import hashlib
import logging
import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from oddish.db.storage import get_storage_client
from oddish.workers.launcher.attempt import WarmSource
from oddish.workers.launcher.providers.base import (
    WARM_IMAGE_READY_SECONDS,
    MonitorSandboxStarter,
    SandboxSize,
    warm_up_lifetime_seconds,
)

logger = logging.getLogger(__name__)

PREFIX = "monitor-image-cache"
# A warm-up marker older than the longest live warm-up is abandoned.
WARMING_TTL_SECONDS = warm_up_lifetime_seconds()


class CacheState(StrEnum):
    WARMING = "warming"  # someone is building the snapshot right now
    READY = "ready"  # boot from ``image``
    EMPTY = "empty"  # this task has nothing worth warming
    INVALID = "invalid"  # the provider refused the snapshot: warm again


class CacheEntry(BaseModel):
    state: CacheState
    image: str | None = None
    at: float = Field(default_factory=time.time)

    @property
    def is_fresh_warming(self) -> bool:
        return (
            self.state is CacheState.WARMING
            and time.time() - self.at < WARMING_TTL_SECONDS
        )

    @property
    def is_fresh_ready(self) -> bool:
        """Served no longer than the provider keeps the image."""
        return (
            self.state is CacheState.READY
            and bool(self.image)
            and time.time() - self.at < WARM_IMAGE_READY_SECONDS
        )


@lru_cache(maxsize=4)
def monitor_identity(monitor_image: str) -> str:
    """Changes whenever the monitor's code or image recipe changes, so a snapshot
    taken from an older monitor is never booted by a newer deploy."""
    digest = hashlib.sha256(monitor_image.encode())
    root = Path(monitor_image).parent
    if Path(monitor_image).is_file():
        tracked = [Path(monitor_image), root / "pyproject.toml", root / "uv.lock"]
        tracked += sorted((root / "scripts").glob("mon-*"))
        tracked += sorted((root / "src").rglob("*.py"))
        for path in tracked:
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def cache_key(provider: str, monitor_image: str, task_identity: str) -> str:
    raw = f"{provider}|{monitor_identity(monitor_image)}|{task_identity}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _object(provider: str, key: str) -> str:
    return f"{PREFIX}/{provider}/{key}.json"


async def _read(provider: str, key: str) -> CacheEntry | None:
    storage = get_storage_client()
    if not await storage.object_exists(_object(provider, key)):
        return None
    return CacheEntry.model_validate(
        await storage.download_json(_object(provider, key))
    )


async def _write(provider: str, key: str, entry: CacheEntry) -> None:
    await get_storage_client().upload_bytes(
        entry.model_dump_json().encode(),
        _object(provider, key),
        content_type="application/json",
    )


async def _delete_previous(starter: MonitorSandboxStarter, image: str) -> None:
    """Delete a snapshot the cache will never serve again, on a provider that
    keeps snapshots until told otherwise (Modal's carry a TTL and have no
    ``delete_warm_image``). Best effort: an undeleted snapshot costs storage,
    never a launch."""
    if not hasattr(starter, "delete_warm_image"):
        return
    try:
        await starter.delete_warm_image(image)  # type: ignore[attr-defined]
        logger.info("metric=monitor_image_cache outcome=deleted image=%s", image)
    except Exception:
        logger.exception(
            "metric=monitor_image_cache outcome=delete_failed image=%s", image
        )


async def image_is_gone(starter: MonitorSandboxStarter, image: str) -> bool:
    """True only when the provider says the cached image no longer exists.
    A provider that cannot say keeps the entry: deleting a snapshot other
    launches are booting from is worse than one cold start."""
    probe = getattr(starter, "warm_image_exists", None)
    if probe is None:
        return False
    return await probe(image) is False


async def forget(starter: MonitorSandboxStarter, key: str) -> None:
    """Drop an entry whose snapshot the provider no longer accepts, and the
    snapshot with it."""
    entry = await _read(starter.provider, key)
    if entry and entry.image:
        await _delete_previous(starter, entry.image)
    await _write(starter.provider, key, CacheEntry(state=CacheState.INVALID))


async def resolve(
    starter: MonitorSandboxStarter,
    *,
    monitor_image: str,
    warm_source: WarmSource | None,
    has_registry_login: bool,
    size: SandboxSize | None = None,
) -> tuple[str, str | None]:
    """(image to boot, cache key if it came from the cache).

    A run with a private registry login is never cached: the login must not
    end up inside a snapshot other runs boot from.
    """
    if (
        not warm_source
        or has_registry_login
        or not hasattr(starter, "build_warm_image")
    ):
        return monitor_image, None
    started = time.monotonic()
    # The size, GPU included, is part of the identity: some providers boot a
    # snapshot at the size it was taken at, and a per-trial override changes it.
    sized = (
        f"{warm_source.identity}|{size.cpu}x{size.memory_mb}"
        f"|{size.gpu_type}x{size.gpu_count}"
        if size
        else ""
    )
    key = cache_key(starter.provider, monitor_image, sized or warm_source.identity)
    try:
        entry = await _read(starter.provider, key)
        if entry and entry.is_fresh_ready:
            logger.info("metric=monitor_image_cache outcome=hit key=%s", key)
            return entry.image, key
        if entry and entry.state is CacheState.EMPTY:
            return monitor_image, None
        if entry and entry.is_fresh_warming:
            logger.info(
                "metric=monitor_image_cache outcome=warming_elsewhere key=%s", key
            )
            return monitor_image, None  # someone else is warming: run cold this once
        if entry and entry.image:
            # A READY entry past its serving window: its snapshot is never
            # booted again, so it goes before the replacement is warmed.
            await _delete_previous(starter, entry.image)
        await _write(starter.provider, key, CacheEntry(state=CacheState.WARMING))
        try:
            image = await starter.build_warm_image(  # type: ignore[attr-defined]
                image=monitor_image, request=warm_source.request, size=size
            )
        except Exception:
            # Not EMPTY and not left WARMING: the next trial warms again.
            await forget(starter, key)
            raise
        done = CacheState.READY if image else CacheState.EMPTY
        await _write(starter.provider, key, CacheEntry(state=done, image=image))
        logger.info(
            "metric=monitor_image_cache outcome=%s key=%s seconds=%.1f",
            "warmed" if image else "nothing_to_warm",
            key,
            time.monotonic() - started,
        )
        return (image, key) if image else (monitor_image, None)
    except Exception:
        logger.exception("metric=monitor_image_cache outcome=error key=%s", key)
        return monitor_image, None
