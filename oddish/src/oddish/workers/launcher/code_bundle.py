"""The launcher's own monitor code, for a sandbox booted from a warm snapshot.

A snapshot keeps the monitor code it was warmed with, and its cache key covers
only what the snapshot has to hold (``image_cache.snapshot_base_identity``), so
it outlives code-only deploys. Every snapshot boot therefore gets the code the
launcher runs itself: a deterministic tar.gz of the oddish folder's ``src``,
the entrypoint and the lock, named by the sha256 of its bytes, uploaded once
and presigned per boot. ``scripts/mon-boot.sh`` fetches it inside the sandbox.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import tarfile
import time
from functools import lru_cache
from pathlib import Path

from oddish.db.storage import get_storage_client
from oddish.workers.launcher import image_cache
from oddish.workers.launcher.providers.base import CodeBundle

logger = logging.getLogger(__name__)

PREFIX = f"{image_cache.PREFIX}/bundles"
BOOT_SCRIPT = "scripts/mon-boot.sh"
# The launcher itself writes bytecode into its src; the sandbox recompiles.
_BYTECODE_DIR, _BYTECODE = "__pycache__", (".pyc", ".pyo")
# The sandbox fetches as it boots, after a create that may take the Daytona
# create timeout (1200 s by default).
URL_SECONDS = 3600


def _members(root: Path) -> list[tuple[str, Path]]:
    """``src`` and the snapshot base: a boot installs the entrypoint, and only
    compares pyproject.toml and uv.lock with the snapshot's, whose venv stays."""
    members = [(name, root / name) for name in image_cache.SNAPSHOT_BASE]
    for path in (root / "src").rglob("*"):
        relative = path.relative_to(root)
        if (
            path.is_file()
            and _BYTECODE_DIR not in relative.parts
            and path.suffix not in _BYTECODE
        ):
            members.append((relative.as_posix(), path))
    return sorted(members)


def build(root: Path) -> bytes:
    """The bundle's bytes: equal for equal files, whatever their timestamps,
    owners or bytecode."""
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in _members(root):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)  # uid, gid, mtime 0; no owner names
            info.size = len(data)
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(tar_bytes.getvalue(), compresslevel=6, mtime=0)


@lru_cache(maxsize=2)
def _built(root: str) -> tuple[bytes, str]:
    """Once per launcher process: its code does not change while it runs."""
    data = build(Path(root))
    return data, hashlib.sha256(data).hexdigest()


def object_key(sha256: str) -> str:
    return f"{PREFIX}/{sha256}.tar.gz"


async def prepare(root: Path) -> CodeBundle:
    """Upload the bundle unless the bucket has it (content-addressed, so a race
    uploads the same bytes twice) and presign a fetch for one boot."""
    started = time.monotonic()
    # Off the event loop: the launch's heartbeat shares it.
    data, sha256 = await asyncio.to_thread(_built, str(root))
    storage = get_storage_client()
    key = object_key(sha256)
    uploaded = not await storage.object_exists(key)
    if uploaded:
        await storage.upload_bytes(data, key, content_type="application/gzip")
    url = await storage.get_presigned_url(key, expiration=URL_SECONDS)
    logger.info(
        "metric=monitor_code_bundle bundle=%s bytes=%d uploaded=%s seconds=%.2f",
        sha256,
        len(data),
        uploaded,
        time.monotonic() - started,
    )
    return CodeBundle(
        sha256=sha256, url=url, boot_script=(root / BOOT_SCRIPT).read_text()
    )
