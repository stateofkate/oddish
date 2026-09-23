"""Fetch the task directory the way the spec says: one archive, or loose files."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx

from oddish.mon.spec import AttemptSpec


class TaskFetchError(RuntimeError):
    pass


def _safe_target(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise TaskFetchError(f"Refusing path outside the task directory: {relative}")
    return target


async def fetch_task(
    spec: AttemptSpec, dest: Path, *, transport: httpx.AsyncBaseTransport | None = None
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
