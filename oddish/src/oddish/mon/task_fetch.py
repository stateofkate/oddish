"""Fetch the task directory the way the spec says: one archive, or loose files.

A warm-up keeps its copy in the snapshot, under the name of exactly those files
(``task_files_version``); a monitor booted from that snapshot uses the copy instead of
fetching when its spec names the same files.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tarfile
import tempfile
from pathlib import Path

import httpx

from oddish.mon.contract import TASK_FILES_VERSION_LENGTH, AttemptSpec, WarmRequest

logger = logging.getLogger(__name__)

# On the sandbox's own disk (not a volume), so a snapshot keeps it.
KEPT_TASKS_VAR = "ODDISH_MON_KEPT_TASKS_DIR"
DEFAULT_KEPT_TASKS_DIR = "/var/lib/oddish-mon/tasks"

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION = re.compile(f"[0-9a-f]{{{TASK_FILES_VERSION_LENGTH}}}")


class TaskFetchError(RuntimeError):
    pass


def _safe_target(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise TaskFetchError(f"Refusing path outside the task directory: {relative}")
    return target


def _task_dir_prefix(task_id: str) -> str:
    """Named as the worker names it (storage.resolve_task_directory), so Harbor
    derives the same kind of trial folder name on every path."""
    return f"task-{task_id}-"


async def fetch_task(
    spec: AttemptSpec | WarmRequest,
    dest: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if not spec.task_archive_url and not spec.task_files:
        raise TaskFetchError("The attempt spec has nothing to fetch for this task")
    async with httpx.AsyncClient(timeout=300.0, transport=transport) as http:
        if spec.task_archive_url:
            response = await http.get(spec.task_archive_url)
            response.raise_for_status()
            with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
                tar.extractall(dest, filter="data")
            return dest
        for relative, url in spec.task_files.items():
            response = await http.get(url)
            response.raise_for_status()
            target = _safe_target(dest, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
    return dest


def kept_task_path(task_id: str | None, version: str | None) -> Path | None:
    """Where a warm-up keeps this version of the task. The last part is shaped
    like the directory the monitor fetches into (``task-<task id>-<suffix>``),
    so Harbor derives the same kind of trial name either way. None when there
    is no version or the path cannot be built safely."""
    if not task_id or not version or not _VERSION.fullmatch(version):
        return None
    if not _TASK_ID.fullmatch(task_id):
        logger.warning("no kept copy for task_id=%r: not a safe dir name", task_id)
        return None
    root = Path(os.environ.get(KEPT_TASKS_VAR) or DEFAULT_KEPT_TASKS_DIR)
    return root / version / f"{_task_dir_prefix(task_id)}{version[:8]}"


def kept_task(spec: AttemptSpec) -> Path | None:
    """The snapshot's copy of exactly the files this spec names, or None."""
    path = kept_task_path(spec.task_id, spec.task_files_version)
    # The version directory appears in one rename, after a complete fetch.
    return path if path is not None and path.is_dir() else None


async def fetch_or_keep(spec: AttemptSpec, workdir: Path) -> Path:
    """The monitor's fetch: the warm snapshot's copy of exactly these files if
    it has one, since the warm-up built from that path and the trial's build
    then hits its layers; otherwise a fresh fetch under ``workdir``."""
    path = kept_task(spec)
    source = "kept" if path else "fetched"
    if path is None:
        task_dir = tempfile.mkdtemp(prefix=_task_dir_prefix(spec.task_id), dir=workdir)
        path = await fetch_task(spec, Path(task_dir))
    logger.info(
        "metric=monitor_task_files source=%s task_id=%s path=%s",
        source,
        spec.task_id,
        path,
    )
    return path


async def fetch_and_keep(request: WarmRequest, fallback: Path) -> Path:
    """The warm-up's fetch: into the kept path when the request names its
    files, so the snapshot holds them and a trial builds from the same path;
    otherwise into ``fallback``, as before."""
    path = kept_task_path(request.task_id, request.task_files_version)
    if path is None:
        return await fetch_task(request, fallback)
    if not path.is_dir():
        version_dir = path.parent
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(
            tempfile.mkdtemp(prefix=f".{version_dir.name}-", dir=version_dir.parent)
        )
        partial.chmod(0o755)
        await fetch_task(request, partial / path.name)
        partial.rename(version_dir)
    return path
