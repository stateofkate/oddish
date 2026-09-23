"""``python -m oddish.mon warm``: prepare task images before the disk is snapshotted.

Runs in a warm-up sandbox that never holds a monitor token, a registry login or
model keys, and never runs an agent. Its only input is where to fetch the task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import tomllib
from pathlib import Path

from oddish.mon import task_fetch, task_image
from oddish.mon.spec import WarmRequest

logger = logging.getLogger(__name__)

WARM_VAR = "ODDISH_MON_WARM"  # a WarmRequest as JSON
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)
_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)


def image_refs(task_dir: Path, docker_image: str | None = None) -> list[str]:
    """Images a trial of this task would pull. A prebuilt image replaces the build."""
    if docker_image:
        return [docker_image]
    env_dir = task_dir / "environment"
    refs: list[str] = []
    try:
        config = tomllib.loads((task_dir / "task.toml").read_text())
        declared = (config.get("environment") or {}).get("docker_image")
        if declared:
            return [str(declared)]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    dockerfile = env_dir / "Dockerfile"
    if dockerfile.is_file():
        stages: set[str] = set()
        for ref, alias in _FROM.findall(dockerfile.read_text(errors="replace")):
            if ref.lower() != "scratch" and ref not in stages and "$" not in ref:
                refs.append(ref)
            if alias:
                stages.add(alias)
    for compose in sorted(env_dir.glob("*compose*.y*ml")):
        refs += [r for r in _COMPOSE_IMAGE.findall(compose.read_text(errors="replace")) if "$" not in r]
    return list(dict.fromkeys(refs))


async def _pull(ref: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "docker", "pull", "-q", ref,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0:
        logger.warning("could not pull %s: %s", ref, err.decode(errors="replace")[-200:])
    return process.returncode == 0


async def run() -> int:
    """0 when an image was built or pulled (worth a snapshot), 1 otherwise."""
    request = WarmRequest.model_validate_json(os.environ.pop(WARM_VAR, "") or "{}")
    with tempfile.TemporaryDirectory(prefix="oddish-mon-warm-") as tmp:
        task_dir = await task_fetch.fetch_task(request, Path(tmp) / "task")
        if not request.docker_image and await task_image.build(task_dir):
            return 0
        refs = image_refs(task_dir, request.docker_image)
    pulled = [ref for ref in refs if await _pull(ref)]
    logger.info("warm-up pulled %d of %d image(s): %s", len(pulled), len(refs), ", ".join(pulled))
    return 0 if pulled else 1
