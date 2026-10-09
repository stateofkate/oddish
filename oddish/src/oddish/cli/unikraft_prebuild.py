"""Build and push Unikraft task images before a hosted submission.

Hosted workers have no BuildKit and run with ``require_prebuilt_image``, so the
machine that runs ``oddish run`` builds each image first. The image ref is the
one Harbor derives: the content hash of ``environment/`` under the fixed
``harbor-task`` repository, so both sides compute it from the same inputs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console

from harbor.environments.definition import (
    SNAPSHOT_HASH_LEN,
    environment_content_hash,
    should_use_prebuilt_docker_image,
)
from harbor.models.task.config import TaskConfig as HarborTaskConfig


console = Console()
error_console = Console(stderr=True)

#: Mirrors ``harbor.environments.unikraft.UNIKRAFT_CLI_ENV``.
UNIKRAFT_CLI_ENV = "HARBOR_UNIKRAFT_CLI"
DEFAULT_ARCH = "x86_64"


def _task_config(task_path: Path) -> HarborTaskConfig:
    return HarborTaskConfig.model_validate_toml((task_path / "task.toml").read_text())


def task_image_ref(task_path: Path, *, namespace: str) -> str:
    """The ``<namespace>/harbor-task:<hash>`` ref Harbor will look for."""
    docker_image = _task_config(task_path).environment.docker_image
    tag = environment_content_hash(
        task_path / "environment", docker_image=docker_image
    )[:SNAPSHOT_HASH_LEN]
    return f"{namespace}/harbor-task:{tag}"


def analysis_image_refs(namespace: str) -> dict[str, str]:
    """The ref of each fixed analysis image, mapped to its Dockerfile text.
    Each ref hashes the environment ``apply_analysis_overlay`` stages."""
    from oddish.worker.probe_staging import ANALYSIS_DOCKERFILES

    refs: dict[str, str] = {}
    for dockerfile_text in ANALYSIS_DOCKERFILES:
        with tempfile.TemporaryDirectory(prefix="oddish-unikraft-") as tmp:
            env_dir = Path(tmp) / "environment"
            env_dir.mkdir()
            (env_dir / "Dockerfile").write_text(dockerfile_text)
            tag = environment_content_hash(env_dir, docker_image=None)[
                :SNAPSHOT_HASH_LEN
            ]
        refs[f"{namespace}/harbor-task:{tag}"] = dockerfile_text
    return refs


def _cli() -> str:
    cli = os.environ.get(UNIKRAFT_CLI_ENV) or shutil.which("unikraft")
    if not cli:
        error_console.print(
            "[red]--env unikraft needs the unikraft CLI to build the task "
            f"image: install it, or point {UNIKRAFT_CLI_ENV} at the binary.[/red]"
        )
        raise typer.Exit(1)
    return cli


def _image_exists(cli: str, ref: str) -> bool:
    """Whether the registry holds the image. A failed check only costs a rebuild."""
    result = subprocess.run([cli, "images", "get", ref], capture_output=True, text=True)
    return result.returncode == 0


def _build(cli: str, build_dir: Path, ref: str, *, quiet: bool) -> None:
    # Needs unikraft CLI >= 0.6.1: a bare Dockerfile builds an EROFS rootfs.
    command = [cli, "build", str(build_dir), "--arch", DEFAULT_ARCH, "-o", ref]
    result = subprocess.run(command, capture_output=quiet, text=True)
    if result.returncode != 0:
        tail = ((result.stderr or result.stdout or "") if quiet else "")[-4000:]
        error_console.print(
            f"[red]unikraft build failed for {ref} (exit {result.returncode})[/red]"
        )
        if tail:
            error_console.print(tail)
        raise typer.Exit(1)


def _require_namespace() -> str:
    # The hosted worker reads its namespace only from its own UKC_USER, so a
    # locally derived name can point the push where no worker ever looks.
    namespace = os.environ.get("UKC_USER")
    if not namespace:
        error_console.print(
            "[red]--env unikraft needs UKC_USER (the registry namespace) so the "
            "image name matches what the worker will look for.[/red]"
        )
        raise typer.Exit(1)
    return namespace


def prebuild_task_images(
    task_paths: list[Path], *, quiet: bool = False, force_build: bool = False
) -> None:
    """Ensure every task's image exists in the registry, building what is missing.
    ``force_build`` rebuilds and pushes a task image the registry already has."""
    namespace = _require_namespace()
    cli = _cli()
    # The server creates QA, audit and summarize trials itself, so no
    # submission carries their images; each run pushes the fixed set too.
    _ensure_analysis_images(cli, namespace, quiet=quiet)
    for task_path in task_paths:
        ref = task_image_ref(task_path, namespace=namespace)
        if not force_build and _image_exists(cli, ref):
            if not quiet:
                console.print(f"  [dim]Unikraft image {ref} already exists[/dim]")
            continue
        if not quiet:
            console.print(f"  Building Unikraft image {ref}...")
        environment_dir = task_path / "environment"
        docker_image = _task_config(task_path).environment.docker_image
        if should_use_prebuilt_docker_image(
            environment_dir, docker_image=docker_image, force_build=force_build
        ):
            # A prebuilt image is imported through a one-line Dockerfile, as
            # Harbor does.
            with tempfile.TemporaryDirectory(prefix="oddish-unikraft-") as tmp:
                (Path(tmp) / "Dockerfile").write_text(f"FROM {docker_image}\n")
                _build(cli, Path(tmp), ref, quiet=quiet)
        else:
            _build(cli, environment_dir, ref, quiet=quiet)


def _ensure_analysis_images(cli: str, namespace: str, *, quiet: bool) -> None:
    """Push each missing analysis image. Existing images cost one lookup each."""
    for ref, dockerfile_text in analysis_image_refs(namespace).items():
        if _image_exists(cli, ref):
            if not quiet:
                console.print(f"  [dim]Unikraft analysis image {ref} exists[/dim]")
            continue
        if not quiet:
            console.print(f"  Building Unikraft analysis image {ref}...")
        with tempfile.TemporaryDirectory(prefix="oddish-unikraft-") as tmp:
            (Path(tmp) / "Dockerfile").write_text(dockerfile_text)
            _build(cli, Path(tmp), ref, quiet=quiet)
