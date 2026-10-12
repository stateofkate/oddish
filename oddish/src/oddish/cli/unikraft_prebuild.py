"""Build and push Unikraft task images before a hosted submission.

Hosted workers have no BuildKit and run with ``require_prebuilt_image``, so the
machine that runs ``oddish run`` builds each image first. The image ref is the
one Harbor derives: the content hash of ``environment/`` under the fixed
``harbor-task`` repository, so both sides compute it from the same inputs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console

from harbor.environments.definition import (
    COMPOSE_FILE_NAME,
    SNAPSHOT_HASH_LEN,
    environment_content_hash,
    has_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.models.task.config import StepConfig
from harbor.models.task.config import TaskConfig as HarborTaskConfig
from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config


console = Console()
error_console = Console(stderr=True)

#: Mirrors ``harbor.environments.unikraft.UNIKRAFT_CLI_ENV``.
UNIKRAFT_CLI_ENV = "HARBOR_UNIKRAFT_CLI"
DEFAULT_ARCH = "x86_64"
#: Mirrors Harbor's ``DEFAULT_DIND_IMAGE``: the VM image of a Compose task.
DEFAULT_DIND_IMAGE = "docker:28.3.3-dind"


def _task_config(task_path: Path) -> HarborTaskConfig:
    return HarborTaskConfig.model_validate_toml((task_path / "task.toml").read_text())


def _env_image_ref(context: Path, docker_image: str | None, namespace: str) -> str:
    """The ``<namespace>/harbor-task:<hash>`` ref Harbor derives from a build context."""
    tag = environment_content_hash(context, docker_image=docker_image)[
        :SNAPSHOT_HASH_LEN
    ]
    return f"{namespace}/harbor-task:{tag}"


def task_image_ref(task_path: Path, *, namespace: str) -> str:
    """The ``<namespace>/harbor-task:<hash>`` ref Harbor will look for."""
    docker_image = _task_config(task_path).environment.docker_image
    return _env_image_ref(task_path / "environment", docker_image, namespace)


def _is_compose_context(context: Path) -> bool:
    """Whether Harbor runs this environment with Docker Compose in a DinD VM."""
    return (context / COMPOSE_FILE_NAME).exists()


def _verifier_build_context(task_path: Path, step_cfg: StepConfig | None) -> Path:
    """Where Harbor reads a separate verifier's environment, as its trial does."""
    if step_cfg is not None:
        step_tests = task_path / "steps" / step_cfg.name / "tests"
        if step_tests.exists():
            return step_tests
    return task_path / "tests"


def _env_build_targets(task_path: Path) -> list[tuple[Path, str | None]]:
    """Each (build context, docker_image) that the worker needs: the agent
    environment, then each separate verifier. A shared verifier adds none."""
    task_cfg = _task_config(task_path)
    targets: list[tuple[Path, str | None]] = [
        (task_path / "environment", task_cfg.environment.docker_image)
    ]
    seen: set[tuple[Path, str | None]] = set()
    for step_cfg in task_cfg.steps or [None]:
        env_config = resolve_effective_verifier_env_config(task_cfg, step_cfg)
        if env_config is None:
            continue
        context = _verifier_build_context(task_path, step_cfg)
        if not has_agent_environment_definition(
            context, docker_image=env_config.docker_image
        ):
            continue
        key = (context.resolve(), env_config.docker_image)
        if key in seen:
            continue
        seen.add(key)
        targets.append((context, env_config.docker_image))
    return targets


def dind_image_ref(namespace: str) -> tuple[str, str]:
    """The ``<namespace>/harbor-dind:<tag>`` ref Harbor looks for, and its Dockerfile."""
    dockerfile_text = f"FROM {DEFAULT_DIND_IMAGE}\n"
    tag = hashlib.sha256(dockerfile_text.encode()).hexdigest()[:SNAPSHOT_HASH_LEN]
    return f"{namespace}/harbor-dind:{tag}", dockerfile_text


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
    targets = {task_path: _env_build_targets(task_path) for task_path in task_paths}
    if any(
        _is_compose_context(context)
        for task_targets in targets.values()
        for context, _ in task_targets
    ):
        _ensure_image(cli, *dind_image_ref(namespace), what="DinD", quiet=quiet)
    for task_path, task_targets in targets.items():
        for context, docker_image in task_targets:
            if _is_compose_context(context):
                # The VM builds the services; it only needs the DinD image.
                continue
            label = "task" if context.name == "environment" else "verifier"
            _ensure_env_image(
                cli,
                namespace,
                context,
                docker_image,
                force_build=force_build,
                quiet=quiet,
                label=label,
            )


def _ensure_analysis_images(cli: str, namespace: str, *, quiet: bool) -> None:
    """Push each missing analysis image. Existing images cost one lookup each."""
    for ref, dockerfile_text in analysis_image_refs(namespace).items():
        _ensure_image(cli, ref, dockerfile_text, what="analysis", quiet=quiet)


def _ensure_env_image(
    cli: str,
    namespace: str,
    context: Path,
    docker_image: str | None,
    *,
    force_build: bool,
    quiet: bool,
    label: str,
) -> None:
    """Build and push the image Harbor derives from a build context, unless the
    registry already has it. Mirrors Harbor's own build vs prebuilt-wrap choice."""
    ref = _env_image_ref(context, docker_image, namespace)
    if not force_build and _image_exists(cli, ref):
        if not quiet:
            console.print(f"  [dim]Unikraft {label} image {ref} already exists[/dim]")
        return
    if not quiet:
        console.print(f"  Building Unikraft {label} image {ref}...")
    if should_use_prebuilt_docker_image(
        context, docker_image=docker_image, force_build=force_build
    ):
        # A prebuilt image is imported through a one-line Dockerfile, as Harbor does.
        with tempfile.TemporaryDirectory(prefix="oddish-unikraft-") as tmp:
            (Path(tmp) / "Dockerfile").write_text(f"FROM {docker_image}\n")
            _build(cli, Path(tmp), ref, quiet=quiet)
    else:
        _build(cli, context, ref, quiet=quiet)


def _ensure_image(
    cli: str, ref: str, dockerfile_text: str, *, what: str, quiet: bool
) -> None:
    """Push ``ref`` from a one-file build context, unless the registry has it."""
    if _image_exists(cli, ref):
        if not quiet:
            console.print(f"  [dim]Unikraft {what} image {ref} exists[/dim]")
        return
    if not quiet:
        console.print(f"  Building Unikraft {what} image {ref}...")
    with tempfile.TemporaryDirectory(prefix="oddish-unikraft-") as tmp:
        (Path(tmp) / "Dockerfile").write_text(dockerfile_text)
        _build(cli, Path(tmp), ref, quiet=quiet)
