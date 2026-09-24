"""``python -m oddish.mon warm``: prepare task images before the disk is snapshotted.

Runs in a warm-up sandbox that never holds a monitor token, a registry login or
model keys, and never runs an agent or starts a task container. Its only input
is where to fetch the task. The fetched task stays in the snapshot when the
request names its files, and every image a trial of it would build is built.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from oddish.mon import compose_build, task_fetch, task_image
from oddish.mon.contract import NOTHING_TO_WARM, WARM_FAILED, WARM_VAR, WarmRequest

logger = logging.getLogger(__name__)

_FROM = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?",
    re.IGNORECASE | re.MULTILINE,
)
_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)


def image_refs(task_dir: Path, docker_image: str | None = None) -> list[str]:
    """Images a trial of this task would pull. A prebuilt image replaces the build."""
    if docker_image:
        return [docker_image]
    env_dir = task_dir / "environment"
    refs: list[str] = []
    config = task_image.task_config(task_dir)
    if config is not None and config.environment.docker_image:
        return [config.environment.docker_image]
    dockerfile = env_dir / "Dockerfile"
    if dockerfile.is_file():
        stages: set[str] = set()
        for ref, alias in _FROM.findall(dockerfile.read_text(errors="replace")):
            if ref.lower() != "scratch" and ref not in stages and "$" not in ref:
                refs.append(ref)
            if alias:
                stages.add(alias)
    for compose in sorted(env_dir.glob("*compose*.y*ml")):
        refs += [
            r
            for r in _COMPOSE_IMAGE.findall(compose.read_text(errors="replace"))
            if "$" not in r
        ]
    return list(dict.fromkeys(refs))


async def _pull(ref: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "pull",
        "-q",
        ref,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "could not pull %s: %s", ref, err.decode(errors="replace")[-200:]
        )
    return process.returncode == 0


async def run() -> int:
    """Fetch the task, build what a trial would build and pull what it would
    pull: 0 when anything was built or pulled (worth a snapshot), otherwise
    ``NOTHING_TO_WARM`` or ``WARM_FAILED``."""
    request = WarmRequest.model_validate_json(os.environ.pop(WARM_VAR, "") or "{}")
    with tempfile.TemporaryDirectory(prefix="oddish-mon-warm-") as tmp:
        task_dir = await task_fetch.fetch_and_keep(request, Path(tmp) / "task")
        sidecar = await compose_build.build_network_sidecar(task_dir)
        if not request.docker_image and await task_image.build(task_dir):
            return 0
        refs = image_refs(task_dir, request.docker_image)
        # Pulled before the build, so the build uses the base images a trial
        # finds in the snapshot; images Compose only runs are pulled here too.
        pulled = [ref for ref in refs if await _pull(ref)]
        built = (
            compose_build.ComposeBuild.NOT_NEEDED
            if request.docker_image
            else await compose_build.build(task_dir)
        )
    if refs:
        logger.info(
            "warm-up pulled %d of %d image(s): %s",
            len(pulled),
            len(refs),
            ", ".join(pulled),
        )
    if built is compose_build.ComposeBuild.BUILT or sidecar:
        return 0
    if not refs and built is compose_build.ComposeBuild.NOT_NEEDED:
        return NOTHING_TO_WARM
    return 0 if pulled else WARM_FAILED
