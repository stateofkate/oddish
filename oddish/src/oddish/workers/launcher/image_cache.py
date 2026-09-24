"""Reuse a task's pulled images across monitor sandboxes.

A fresh sandbox has an empty Docker cache, so every trial of a heavy task would
pull its images again. The first trial instead runs a warm-up sandbox that pulls
them and snapshots its disk; later trials boot from that snapshot.

Provider-neutral: the provider only implements ``build_warm_image``. Entries are
small JSON objects in the storage bucket. A launch waits for another launch's
warm-up rather than boot cold beside it. A warm-up that fails twice boots the
plain image, as does a wait past its cap or a storage error: the cache never
fails a launch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from enum import StrEnum
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field

from oddish.db.storage import get_storage_client
from oddish.mon.contract import WarmRequest
from oddish.workers.launcher import warm_timing
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
# A launch that finds another launch warming reads the entry this often, and
# boots the plain image when there is still no snapshot after
# MAX_WARM_WAIT_SECONDS (that launch may be gone without a trace).
WAIT_POLL_SECONDS = 10
MAX_WARM_WAIT_SECONDS = 1800


class CacheState(StrEnum):
    WARMING = "warming"  # someone is building the snapshot right now
    READY = "ready"  # boot from ``image``
    EMPTY = "empty"  # this task has nothing worth warming
    INVALID = "invalid"  # the provider refused the snapshot: warm again


class ImageOutcome(StrEnum):
    """How a launch got the image it booted: its ``image=``."""

    HIT = "hit"  # a fresh snapshot was in the cache
    WAITED = "waited"  # another launch's warm-up made it while this one waited
    WARMED = "warmed"  # this launch warmed it
    COLD = "cold"  # nothing to cache or warm: the plain image
    WARM_FAILED = "warm_failed"  # the warm-up failed twice: the plain image
    WAIT_EXPIRED = "wait_expired"  # the wait passed its cap: the plain image
    ERROR = "error"  # the cache could not be read or written: the plain image
    GONE = "gone"  # set by the launch: the snapshot had to be warmed again
    NONE = "none"  # set by the launch: it ended before the lookup


class Resolved(NamedTuple):
    image: str  # what to boot
    key: str | None  # the cache key, when the image came from the cache
    outcome: ImageOutcome


class CacheEntry(BaseModel):
    state: CacheState
    image: str | None = None
    at: float = Field(default_factory=time.time)
    # The launch that wrote a WARMING marker: only it may clear that marker.
    owner: str | None = None

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


# Beside mon.Dockerfile, how a snapshot was made: the entrypoint, whose warm
# branch filled its Docker store (a boot installs the bundle's copy), and the
# lock its venv was installed from (a boot only compares the bundle's).
SNAPSHOT_BASE = ("scripts/mon-entrypoint.sh", "pyproject.toml", "uv.lock")
# Every file ``python -m oddish.mon warm`` imports: how the snapshot's task
# images were made. A test fails when the warm-up imports a file not listed.
# A listed file a tree does not have hashes as nothing.
WARM_RECIPE = (
    "src/oddish/__init__.py",
    "src/oddish/mon/__init__.py",
    "src/oddish/mon/__main__.py",
    "src/oddish/mon/builder.py",
    "src/oddish/mon/compose_build.py",
    "src/oddish/mon/contract.py",
    "src/oddish/mon/env.py",
    "src/oddish/mon/exit_codes.py",
    "src/oddish/mon/status.py",
    "src/oddish/mon/task_fetch.py",
    "src/oddish/mon/task_image.py",
    "src/oddish/mon/warm.py",
)
# Bump to warm every snapshot again when a change outside the files above
# alters what a snapshot holds (a provider's build_warm_image, the warm request).
SNAPSHOT_EPOCH = 1


def source_root(monitor_image: str) -> Path | None:
    """The oddish folder the monitor image is built from, when the launcher
    has it (``/oddish/mon.Dockerfile`` in the worker image): what the snapshot
    key hashes and ``code_bundle`` packs. ``None`` for an image reference,
    keyed by its name alone; a snapshot of it boots the code that image was
    built with."""
    dockerfile = Path(monitor_image)
    return dockerfile.parent if dockerfile.is_file() else None


@lru_cache(maxsize=4)
def snapshot_base_identity(monitor_image: str) -> str:
    """Changes whenever a snapshot would hold something else: the image recipe,
    the entrypoint, the locked dependencies or the warm-up. The rest of the
    monitor's code is left out, so snapshots survive code-only deploys: a
    snapshot boot runs the launcher's own code (``code_bundle``)."""
    digest = hashlib.sha256(f"{SNAPSHOT_EPOCH}|{monitor_image}".encode())
    root = source_root(monitor_image)
    if root is not None:
        for relative in (Path(monitor_image).name, *SNAPSHOT_BASE, *WARM_RECIPE):
            path = root / relative
            if path.is_file():
                digest.update(relative.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def cache_key(provider: str, monitor_image: str, task_identity: str) -> str:
    raw = f"{provider}|{snapshot_base_identity(monitor_image)}|{task_identity}"
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


async def _abandon(starter: MonitorSandboxStarter, key: str, owner: str) -> None:
    """Clear this launch's own WARMING marker. Another launch may have warmed
    the same key meanwhile (both missed the marker): its entry is left alone."""
    entry = await _read(starter.provider, key)
    if entry and entry.state is CacheState.WARMING and entry.owner == owner:
        await _write(starter.provider, key, CacheEntry(state=CacheState.INVALID))


async def _request(
    source: WarmSource,
    fresh_source: Callable[[], Awaitable[WarmSource | None]] | None,
) -> WarmRequest:
    """The warm-up's request with task links signed now: a retry, or a warm-up
    after a long wait, must not fetch with links that may have expired."""
    fresh = await fresh_source() if fresh_source else None
    return (fresh or source).request


async def resolve(
    starter: MonitorSandboxStarter,
    *,
    monitor_image: str,
    warm_source: WarmSource | None,
    has_registry_login: bool,
    size: SandboxSize | None = None,
    fresh_source: Callable[[], Awaitable[WarmSource | None]] | None = None,
) -> Resolved:
    """The image to boot, its cache key if it came from the cache, and how.

    A run with a private registry login is never cached: the login must not
    end up inside a snapshot other runs boot from. A warm-up another launch
    is running is waited for, and a warm-up here is tried twice.
    """
    if (
        not warm_source
        or has_registry_login
        or not hasattr(starter, "build_warm_image")
    ):
        return Resolved(monitor_image, None, ImageOutcome.COLD)
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
        waited = bool(entry and entry.is_fresh_warming)
        while entry and entry.is_fresh_warming:  # someone else is warming
            if time.monotonic() - started >= MAX_WARM_WAIT_SECONDS:
                logger.warning(
                    "metric=monitor_image_cache outcome=wait_expired key=%s", key
                )
                return Resolved(monitor_image, None, ImageOutcome.WAIT_EXPIRED)
            await asyncio.sleep(WAIT_POLL_SECONDS)
            entry = await _read(starter.provider, key)
        if waited:
            logger.info(
                "metric=monitor_image_cache outcome=waited key=%s seconds=%.1f",
                key,
                time.monotonic() - started,
            )
        if entry and entry.is_fresh_ready:
            logger.info("metric=monitor_image_cache outcome=hit key=%s", key)
            return Resolved(
                entry.image, key, ImageOutcome.WAITED if waited else ImageOutcome.HIT
            )
        if entry and entry.state is CacheState.EMPTY:
            return Resolved(monitor_image, None, ImageOutcome.COLD)
        if entry and entry.image:
            # A READY entry past its serving window: its snapshot is never
            # booted again, so it goes before the replacement is warmed.
            await _delete_previous(starter, entry.image)
        owner = uuid.uuid4().hex
        try:
            # Inside the try: a cancel during this write still abandons it.
            await _write(
                starter.provider,
                key,
                CacheEntry(state=CacheState.WARMING, owner=owner),
            )
            for attempt in (1, 2):
                timing = warm_timing.begin(key, starter.provider, attempt)
                try:
                    image = await starter.build_warm_image(  # type: ignore[attr-defined]
                        image=monitor_image,
                        request=await _request(warm_source, fresh_source),
                        size=size,
                    )
                    timing.emit("snapshot" if image else "nothing_to_warm")
                    break
                except asyncio.CancelledError:
                    timing.emit("cancelled")
                    raise
                except Exception:
                    timing.emit("failed")
                    if attempt == 2:
                        # Failed twice: boot the plain image. Not EMPTY and not
                        # left WARMING: the next trial warms again.
                        logger.exception(
                            "metric=monitor_image_cache outcome=warm_failed key=%s",
                            key,
                        )
                        await _abandon(starter, key, owner)
                        return Resolved(monitor_image, None, ImageOutcome.WARM_FAILED)
                    logger.warning(
                        "metric=monitor_image_cache outcome=warm_retry key=%s",
                        key,
                        exc_info=True,
                    )
            done = CacheState.READY if image else CacheState.EMPTY
            await _write(starter.provider, key, CacheEntry(state=done, image=image))
        except (Exception, asyncio.CancelledError):
            # Cut short (a cancel, a storage error): never leave WARMING, or
            # every other launch of the task waits on it. Shielded from a second
            # cancel; a failed write is logged, not raised, so a cancel is never
            # turned into the storage-error return below.
            try:
                await asyncio.shield(_abandon(starter, key, owner))
            except Exception:
                logger.exception(
                    "metric=monitor_image_cache outcome=forget_failed key=%s", key
                )
            raise
        logger.info(
            "metric=monitor_image_cache outcome=%s key=%s seconds=%.1f",
            "warmed" if image else "nothing_to_warm",
            key,
            time.monotonic() - started,
        )
        if image:
            return Resolved(image, key, ImageOutcome.WARMED)
        return Resolved(monitor_image, None, ImageOutcome.COLD)
    except Exception:
        logger.exception("metric=monitor_image_cache outcome=error key=%s", key)
        return Resolved(monitor_image, None, ImageOutcome.ERROR)
