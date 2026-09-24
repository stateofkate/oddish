"""Build and select complete task images stored in a monitor disk snapshot.

Only the standard single-Dockerfile path is eligible. Harbor still owns runtime
mounts, limits, network policy and container lifecycle through its prebuilt path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from harbor.environments.definition import environment_content_hash
from harbor.models.task.config import TaskConfig

from oddish.mon import builder

logger = logging.getLogger(__name__)


def task_config(task: Path) -> TaskConfig | None:
    """The task's ``task.toml`` as Harbor reads it. None when it is missing or
    invalid: Harbor's error to report in the trial, not a failed monitor start
    or warm-up, so no image is prepared for it and the ordinary path runs."""
    try:
        return TaskConfig.model_validate_toml((task / "task.toml").read_text())
    except (OSError, ValueError):
        return None


def image_name(task: Path) -> str | None:
    environment = task / "environment"
    config = task_config(task)
    # Compose can override the build context, args, target and services. Do not
    # replace that contract with a single Docker build. Symlinks are excluded by
    # Harbor's hash, so they cannot safely identify this persisted build either.
    if (
        config is None
        or not (environment / "Dockerfile").is_file()
        or config.environment.docker_image
        or config.environment.kwargs
        or list(environment.glob("*compose*.y*ml"))
        or any(path.is_symlink() for path in environment.rglob("*"))
    ):
        return None
    return "oddish-mon-task:" + environment_content_hash(environment, docker_image=None)


async def build(task: Path) -> bool:
    image = image_name(task)
    if image is None:
        return False
    # Same Dockerfile and context as Harbor's standard compose build. Build
    # before snapshotting, without starting the task or providing trial secrets.
    process = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "--tag",
        image,
        str(task / "environment"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **builder.env_for(task)},
    )
    output, _ = await process.communicate()
    if process.returncode:
        logger.warning(
            "task image build failed: %s", output.decode(errors="replace")[-4000:]
        )
        return False
    logger.info("task image built: %s", image)
    return True


async def reuse(
    task: Path, harbor_config: dict[str, Any] | None
) -> dict[str, Any] | None:
    config = harbor_config or {}
    environment = config.get("environment") or {}
    if (
        config.get("docker_image")
        or environment.get("force_build")
        or environment.get("kwargs")
    ):
        return harbor_config
    image = image_name(task)
    if image is None:
        return harbor_config
    process = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await process.wait() != 0:
        logger.info("task image cache miss: %s", image)
        return harbor_config
    logger.info("task image cache hit: %s; using Harbor prebuilt path", image)
    return {**config, "docker_image": image}
