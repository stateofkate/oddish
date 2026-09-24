"""The warm-up's copy of a trial's image build: Harbor's own ``docker compose
build`` for the task, stopped before any container starts.

A standard single-Dockerfile task gets a complete image instead (``task_image``).
Every other task Harbor builds itself, Compose tasks above all. Running that
build in the warm-up, from the path the trial reuses, with the same Compose
files, overlays, build args and builder, leaves every layer in the snapshot,
so the trial's build is a cache hit. A task that restricts its network also
gets Harbor's egress-control sidecar image, which its trials build otherwise.
Secret-free: the warm-up holds no model key, token or registry login, and the
task's ``${NAME}`` values that need one are left out.

Part of the warm-up's recipe (with ``warm``, ``task_image``, ``task_fetch`` and
``builder``): the monitor's runner does not import it.
"""

from __future__ import annotations

import logging
import os
import tempfile
from enum import StrEnum
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.environments.docker.utils import (
    default_docker_platform,
    ensure_docker_image_built,
)
from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import NetworkMode, TaskConfig
from harbor.models.task.verifier_mode import (
    resolve_step_verifier_mode,
    resolve_task_verifier_mode,
)
from harbor.models.trial.config import AgentConfig, ServiceVolumeConfig
from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.trial.network_policy import resolve_trial_network_plan
from harbor.utils.env import resolve_env_vars

from oddish.mon import builder, task_image

logger = logging.getLogger(__name__)

# The warm-up's Compose project. A project name labels the built images, not
# their layers, so the trial's own project name does not matter to its cache.
WARM_SESSION_ID = "oddish-mon-warm"


class _ContainersWouldStart(Exception):
    """Where Harbor's ``start()`` runs its first Compose command after the build."""


class _BuildOnly(DockerEnvironment):
    """Harbor's own ``start()`` up to its first container command: the Compose
    overlays and ``docker compose build`` a trial runs, and nothing after."""

    built = False

    async def _run_docker_compose_command(self, command, *args, **kwargs):
        if command[:1] != ["build"]:
            raise _ContainersWouldStart(" ".join(command))
        result = await super()._run_docker_compose_command(command, *args, **kwargs)
        self.built = True
        return result


def _secret_free(env: dict[str, str]) -> dict[str, str]:
    """The task's ``[environment.env]`` as this sandbox resolves it. A trial
    resolves ``${NAME}`` from its own environment, which holds the model keys;
    the warm-up holds none, so a name it cannot resolve is left out."""
    resolved: dict[str, str] = {}
    for name, value in env.items():
        try:
            resolved.update(resolve_env_vars({name: value}))
        except ValueError:
            logger.info("warm-up build leaves out %s: not resolvable here", name)
    return resolved


def _log_mounts(
    paths: TrialPaths, container: EnvironmentPaths
) -> list[ServiceVolumeConfig]:
    """The log mounts' targets a trial gives its environment, which a task's
    Compose file may use (after Harbor's ``Trial._agent_env_mounts``, but from
    scratch dirs and without a trial's extra mounts)."""
    return [
        ServiceVolumeConfig(type="bind", source=str(host), target=str(target))
        for host, target in (
            (paths.verifier_dir, container.verifier_dir),
            (paths.agent_dir, container.agent_dir),
            (paths.artifacts_dir, container.artifacts_dir),
        )
    ]


class ComposeBuild(StrEnum):
    BUILT = "built"
    NOT_NEEDED = "not_needed"  # a prebuilt image, or a task task_image builds
    FAILED = "failed"  # reported, not raised: the warm-up must not cache EMPTY


async def build(task: Path) -> ComposeBuild:
    """Run the image build a trial's Docker environment runs for this task and
    stop before any container starts."""
    if task_image.image_name(task) is not None:
        return ComposeBuild.NOT_NEEDED
    config = task_image.task_config(task)
    if config is None:
        logger.info("no warm-up build: task.toml missing or invalid")
        return ComposeBuild.NOT_NEEDED
    environment = config.environment
    if environment.docker_image:
        return ComposeBuild.NOT_NEEDED  # Harbor pulls it; warm.image_refs lists it
    environment = environment.model_copy(update={"env": _secret_free(environment.env)})
    # The runner selects the same builder before Harbor runs (prepare_task).
    os.environ.update(builder.env_for(task))
    with tempfile.TemporaryDirectory(prefix="oddish-mon-warm-trial-") as scratch:
        paths = TrialPaths(Path(scratch))
        paths.mkdir()
        try:
            # Harbor's factory, as for a trial: the task's [environment.kwargs]
            # are the base and a trial's default settings go over them. The
            # class, session_id, environment_name and mounts differ, and there
            # are no network policies: build_network_sidecar builds the sidecar.
            docker_env = EnvironmentFactory.create_environment_from_config(
                config=TrialEnvironmentConfig(import_path=f"{__name__}:_BuildOnly"),
                environment_dir=task / "environment",
                environment_name=task.name,
                session_id=WARM_SESSION_ID,
                trial_paths=paths,
                task_env_config=environment,
                mounts=_log_mounts(paths, EnvironmentPaths.for_os(environment.os)),
            )
            assert isinstance(docker_env, _BuildOnly)
            await docker_env.start(force_build=False)
        except _ContainersWouldStart:
            pass
        except Exception as exc:  # noqa: BLE001 - the trial builds instead
            logger.warning("warm-up build failed: %s", str(exc)[-4000:])
            return ComposeBuild.FAILED
    if not docker_env.built:
        return ComposeBuild.NOT_NEEDED
    logger.info("task images built as a trial builds them")
    return ComposeBuild.BUILT


def restricts_network(config: TaskConfig) -> bool:
    """Whether a trial of this task, as the task configures it, turns on
    Harbor's egress control (``DockerEnvironment._requires_egress_control``):
    some policy in its network plan is not public. A trial-level override can
    still restrict a public task; that trial builds the sidecar itself."""
    for step in config.steps or [None]:
        mode = (
            resolve_step_verifier_mode(config, step)
            if step is not None
            else resolve_task_verifier_mode(config)
        )
        plan = resolve_trial_network_plan(
            config, AgentConfig(), TrialEnvironmentConfig(), step, verifier_mode=mode
        )
        policies = (
            plan.agent_env_baseline,
            plan.agent_phase,
            plan.verifier_env_baseline,
            plan.verifier_phase,
        )
        if any(
            p is not None and p.network_mode != NetworkMode.PUBLIC for p in policies
        ):
            return True
    return False


async def build_network_sidecar(task: Path) -> bool:
    """Harbor's egress-control sidecar image, which a trial of a task with a
    restricted network builds before its own images. The name is a hash of
    Harbor's sidecar files and the platform, so the trial finds this image and
    skips its build. False when the task needs none or the build failed."""
    config = task_image.task_config(task)
    if config is None:
        return False
    try:
        if not restricts_network(config):
            return False
        image = await ensure_docker_image_built(
            docker_name=DockerEnvironment._EGRESS_CONTROL_SIDECAR_DOCKER_NAME,
            docker_build_context=DockerEnvironment._EGRESS_CONTROL_SIDECAR_CONTEXT_PATH,
            dockerfile_path=DockerEnvironment._egress_control_sidecar_dockerfile_path(),
            build_args={},
            platform=await default_docker_platform(),
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 - the trial builds it instead
        logger.warning("egress sidecar not built: %s", str(exc)[-4000:])
        return False
    logger.info("egress sidecar built: %s", image)
    return True
