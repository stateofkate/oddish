"""The warm-up builds the images a trial would build, the way the trial builds
them: Harbor's own Compose build, stopped before any container starts.

Regression: Compose tasks (coredns, gitea on staging) only had their base images
pulled by the warm-up, so every trial built its services in the billed sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.environments.docker import COMPOSE_BUILD_PATH
from harbor.environments.docker.docker import DockerEnvironment

from oddish.mon import builder, compose_build, task_fetch, warm
from oddish.mon.contract import WARM_FAILED, WARM_VAR, WarmRequest

COMPOSE = """\
services:
  main:
    build:
      args:
        APP_MODE: ${APP_MODE}
    volumes:
      - ${HOST_VERIFIER_LOGS_PATH}:${ENV_VERIFIER_LOGS_PATH}
  web:
    build: ./web
  cache:
    image: redis:7-alpine
"""

TASK_TOML = """\
version = "1.0"
[agent]
timeout_sec = 60.0
[verifier]
timeout_sec = 60.0
[environment]
cpus = 2
memory_mb = 4096
[environment.env]
APP_MODE = "prod"
MODEL_TOKEN = "${MODEL_TOKEN}"
"""

# Overlays Harbor writes per start into its own temporary directories. They set
# mounts, limits and the main container's env; none of them defines a build.
_PER_START_OVERLAYS = (
    "docker-compose-resources.json",
    "docker-compose-environment.json",
    "docker-compose-mounts.json",
)


def _write_task(
    root: Path, *, compose: str | None = COMPOSE, toml: str = TASK_TOML
) -> Path:
    environment = root / "environment"
    (environment / "web").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "solution").mkdir()
    (root / "task.toml").write_text(toml)
    (root / "instruction.md").write_text("Do the thing.\n")
    (root / "tests" / "test.sh").write_text("echo ok\n")
    (root / "solution" / "solve.sh").write_text("echo solved\n")
    (environment / "Dockerfile").write_text(
        "FROM alpine:3.20\nARG APP_MODE\nRUN echo $APP_MODE > /mode\nCOPY payload /payload\n"
    )
    (environment / "payload").write_text("one")
    (environment / "web" / "Dockerfile").write_text("FROM alpine:3.20\nRUN echo web\n")
    if compose is not None:
        (environment / "docker-compose.yaml").write_text(compose)
    return root


@pytest.fixture
def compose_task(tmp_path):
    return _write_task(tmp_path / "task-t1-0123abcd")


class _Docker:
    """Records every subprocess; ``docker compose`` exits ``exit_code``."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.exit_code = 0

    async def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, env=kwargs.get("env")))
        code = self.exit_code if args[:2] == ("docker", "compose") else 0

        async def communicate(input=None):
            return b"", b""

        async def wait():
            return code

        class _Lines:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        return SimpleNamespace(
            returncode=code,
            communicate=communicate,
            wait=wait,
            stdout=_Lines(),
            stdin=None,
        )

    @property
    def compose(self) -> list[SimpleNamespace]:
        return [call for call in self.calls if call.args[:2] == ("docker", "compose")]


@pytest.fixture
def docker(monkeypatch):
    fake = _Docker()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    # Harbor asks the daemon for its OS with a blocking `docker info`.
    monkeypatch.setattr(
        DockerEnvironment, "_detect_daemon_os", staticmethod(lambda: "linux")
    )
    # The build sets the builder names in os.environ, as the runner does: the
    # setenv records each name's original state, so teardown restores it.
    for name in ("DOCKER_BUILDKIT", "COMPOSE_DOCKER_CLI_BUILD", "MODEL_TOKEN"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return fake


def _files(args: tuple[str, ...]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]


def _option(args: tuple[str, ...], name: str) -> str:
    return args[args.index(name) + 1]


# --- which build the warm-up runs --------------------------------------------


@pytest.mark.asyncio
async def test_a_compose_task_gets_harbors_build_and_no_container_starts(
    compose_task, docker
):
    assert await compose_build.build(compose_task) is compose_build.ComposeBuild.BUILT

    # One Compose command, the build. No down, up or exec: nothing runs.
    assert [call.args[-1] for call in docker.compose] == ["build"]
    args = docker.compose[0].args
    environment = str((compose_task / "environment").resolve())
    assert _option(args, "--project-name") == compose_build.WARM_SESSION_ID
    assert _option(args, "--project-directory") == environment
    builds = [f for f in _files(args) if not f.endswith(_PER_START_OVERLAYS)]
    assert builds == [
        str(COMPOSE_BUILD_PATH.resolve()),
        f"{environment}/docker-compose.yaml",
    ]

    compose_env = docker.compose[0].env
    assert compose_env["CONTEXT_DIR"] == environment
    assert compose_env["APP_MODE"] == "prod"  # the task's build arg
    # The builder the runner selects for this Dockerfile (classic here).
    assert {k: compose_env[k] for k in builder.env_for(compose_task)} == {
        "DOCKER_BUILDKIT": "0",
        "COMPOSE_DOCKER_CLI_BUILD": "0",
    }
    # Harbor's log-mount variables are set, so a Compose file using them loads.
    assert compose_env["ENV_VERIFIER_LOGS_PATH"] == "/logs/verifier"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape, harbor_builds",
    [
        ("compose", True),
        # Not eligible for the complete-image path, still built by Harbor.
        ("dockerfile_with_kwargs", True),
        # The complete-image path (task_image.build) owns these.
        ("dockerfile", False),
        # Harbor pulls a prebuilt image; image_refs pulls it here.
        ("prebuilt", False),
    ],
)
async def test_the_warm_up_builds_what_harbor_would_build(
    tmp_path, docker, shape, harbor_builds
):
    toml = TASK_TOML
    if shape == "dockerfile_with_kwargs":
        toml = TASK_TOML.replace(
            "[environment.env]",
            "[environment.kwargs]\nkeep_containers = false\n[environment.env]",
        )
    elif shape == "prebuilt":
        toml = TASK_TOML.replace(
            "memory_mb = 4096", 'memory_mb = 4096\ndocker_image = "ghcr.io/o/task:1"'
        )
    task = _write_task(
        tmp_path / "task", compose=COMPOSE if shape == "compose" else None, toml=toml
    )

    assert (
        await compose_build.build(task) is compose_build.ComposeBuild.BUILT
    ) is harbor_builds
    assert [call.args[-1] for call in docker.compose] == (
        ["build"] if harbor_builds else []
    )


@pytest.mark.asyncio
async def test_a_failed_build_is_reported_not_raised(compose_task, docker):
    docker.exit_code = 1
    assert await compose_build.build(compose_task) is compose_build.ComposeBuild.FAILED


# --- the trial's build finds the warm-up's layers ----------------------------


@pytest.mark.asyncio
async def test_the_trials_build_is_the_warm_ups_build(
    compose_task, docker, monkeypatch, tmp_path
):
    """Harbor's own Trial, as the monitor runs it on the snapshot's copy of the
    task, builds with the same Compose files, project directory, build args and
    builder as the warm-up. Only the project name (labels on the final images)
    and the per-start overlays (mounts, limits, env) differ, and none of those
    define a build, so every layer is in the cache."""
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
    )
    from harbor.trial.trial import Trial

    assert await compose_build.build(compose_task) is compose_build.ComposeBuild.BUILT
    warm_build = docker.compose[0]
    docker.calls.clear()

    # What runner.prepare_task sets before Harbor runs; a trial also holds keys.
    monkeypatch.setenv("MODEL_TOKEN", "sk-trial-only")
    for name, value in builder.env_for(compose_task).items():
        monkeypatch.setenv(name, value)
    trial = await Trial.create(
        TrialConfig(
            task=TaskConfig(path=compose_task),
            trial_name="task-t1-0123abcd__AbCdEfG",
            trials_dir=tmp_path / "trials",
            agent=AgentConfig(name="oracle"),
            environment=EnvironmentConfig(type="docker"),
        )
    )
    await trial.agent_environment.start(force_build=False)
    trial_build = docker.compose[0]
    assert trial_build.args[-1] == "build"
    assert [call.args[-1] for call in docker.compose][1:3] == [
        "--remove-orphans",
        "--wait",
    ]

    assert _option(trial_build.args, "--project-directory") == _option(
        warm_build.args, "--project-directory"
    )
    assert [
        f for f in _files(trial_build.args) if not f.endswith(_PER_START_OVERLAYS)
    ] == [f for f in _files(warm_build.args) if not f.endswith(_PER_START_OVERLAYS)]
    for name in (
        "CONTEXT_DIR",
        "MAIN_IMAGE_NAME",
        "APP_MODE",
        "DOCKER_BUILDKIT",
        "COMPOSE_DOCKER_CLI_BUILD",
    ):
        assert trial_build.env[name] == warm_build.env[name], name
    assert _option(trial_build.args, "--project-name") != _option(
        warm_build.args, "--project-name"
    )
    # The trial resolves the task's ${MODEL_TOKEN} from its keys; the warm-up
    # has none, so it never enters a snapshot.
    assert trial_build.env["MODEL_TOKEN"] == "sk-trial-only"
    assert "MODEL_TOKEN" not in warm_build.env


# --- warm.run ----------------------------------------------------------------


def _warm_request(monkeypatch, tmp_path, source: Path, **fields) -> WarmRequest:
    request = WarmRequest(
        task_archive_url="https://signed",
        task_id="t1",
        task_files_version="0123abcd" * 4,
        **fields,
    )
    monkeypatch.setenv(WARM_VAR, request.model_dump_json())
    monkeypatch.setenv(task_fetch.KEPT_TASKS_VAR, str(tmp_path / "kept"))

    async def fetch(spec, dest):
        shutil.copytree(source, dest, dirs_exist_ok=True)
        return dest

    monkeypatch.setattr(task_fetch, "fetch_task", fetch)
    return request


@pytest.mark.asyncio
async def test_warm_pulls_then_builds_from_the_kept_task(
    compose_task, docker, monkeypatch, tmp_path
):
    request = _warm_request(monkeypatch, tmp_path, compose_task)

    assert await warm.run() == 0

    kept = task_fetch.kept_task_path(request.task_id, request.task_files_version)
    assert (kept / "environment" / "docker-compose.yaml").is_file()
    commands = [
        call.args[:2] for call in docker.calls if call.args[1] in ("pull", "compose")
    ]
    # Base images and image-only services first, so the build uses what the
    # snapshot keeps; then Harbor's build, from the kept path.
    assert commands == [("docker", "pull"), ("docker", "pull"), ("docker", "compose")]
    assert _option(docker.compose[0].args, "--project-directory") == str(
        (kept / "environment").resolve()
    )


@pytest.mark.asyncio
async def test_warm_falls_back_to_pulled_images_when_the_build_fails(
    compose_task, docker, monkeypatch, tmp_path
):
    _warm_request(monkeypatch, tmp_path, compose_task)
    docker.exit_code = 1
    assert await warm.run() == 0  # base images pulled: still worth a snapshot


@pytest.mark.asyncio
async def test_warm_reports_nothing_to_warm_only_when_nothing_was_built_or_named(
    tmp_path, docker, monkeypatch
):
    # A Compose task whose images are all interpolated names: nothing to pull,
    # but Harbor's build still builds it.
    compose = COMPOSE.replace("redis:7-alpine", "${CACHE_IMAGE}")
    task = _write_task(tmp_path / "src", compose=compose)
    (task / "environment" / "Dockerfile").write_text("ARG BASE\nFROM ${BASE}\n")
    (task / "environment" / "web" / "Dockerfile").write_text("ARG BASE\nFROM ${BASE}\n")
    _warm_request(monkeypatch, tmp_path, task)
    assert await warm.run() == 0

    docker.exit_code = 1
    shutil.rmtree(tmp_path / "kept")
    _warm_request(monkeypatch, tmp_path, task)  # the first run took WARM_VAR
    # Bugbot on #1853: a failed build is not "nothing to warm", which the
    # launcher caches as EMPTY for good.
    assert await warm.run() == WARM_FAILED


def _phases(caplog) -> list[dict[str, float]]:
    return [
        {k: float(v) for k, v in (f.split("=", 1) for f in r.getMessage().split()[1:])}
        for r in caplog.records
        if r.getMessage().startswith("metric=monitor_warm_phases ")
    ]


@pytest.mark.asyncio
async def test_warm_prints_how_long_it_fetched_and_built(
    compose_task, docker, monkeypatch, tmp_path, caplog
):
    """The launcher reads this line from the warm-up's output for
    ``metric=monitor_warm_timing``."""
    caplog.set_level(logging.INFO)
    _warm_request(monkeypatch, tmp_path, compose_task)
    real_fetch = task_fetch.fetch_task

    async def slow_fetch(spec, dest):
        await asyncio.sleep(0.03)
        return await real_fetch(spec, dest)

    monkeypatch.setattr(task_fetch, "fetch_task", slow_fetch)

    assert await warm.run() == 0

    (line,) = _phases(caplog)
    assert list(line) == ["fetch_s", "build_s"]
    assert line["fetch_s"] >= 0.03 and line["build_s"] >= 0


@pytest.mark.asyncio
async def test_a_warm_up_that_fails_still_prints_its_phases(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(
        WARM_VAR, WarmRequest(task_archive_url="https://x").model_dump_json()
    )

    async def fetch_fails(request, dest):
        await asyncio.sleep(0.02)
        raise RuntimeError("403 fetching the task")

    monkeypatch.setattr(task_fetch, "fetch_and_keep", fetch_fails)

    with pytest.raises(RuntimeError, match="403"):
        await warm.run()

    (line,) = _phases(caplog)
    assert line["fetch_s"] >= 0.02 and line["build_s"] == 0


# --- a restricted task's egress sidecar ---------------------------------------

_NETWORK = {
    "public": TASK_TOML,
    "no_network_baseline": TASK_TOML.replace(
        "[environment]\n", '[environment]\nnetwork_mode = "no-network"\n'
    ),
    "restricted_agent_phase": TASK_TOML.replace(
        "[agent]\n", '[agent]\nnetwork_mode = "no-network"\n'
    ),
    "allowlisted_verifier_phase": TASK_TOML.replace(
        "[verifier]\n",
        '[verifier]\nnetwork_mode = "allowlist"\nallowed_hosts = ["pypi.org"]\n',
    ),
}


@pytest.fixture
def sidecar_builds(monkeypatch):
    built: list[dict] = []

    async def ensure_built(**kwargs):
        built.append(kwargs)
        return f"{kwargs['docker_name']}--hash"

    async def platform():
        return "linux/amd64"

    monkeypatch.setattr(compose_build, "ensure_docker_image_built", ensure_built)
    monkeypatch.setattr(compose_build, "default_docker_platform", platform)
    return built


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_NETWORK))
async def test_the_sidecar_is_built_when_a_trial_would_turn_on_egress_control(
    tmp_path, docker, monkeypatch, sidecar_builds, shape
):
    """Harbor's own Trial decides whether its environment runs egress control;
    the warm-up builds the sidecar for exactly those tasks, from Harbor's own
    sidecar files, so the trial's check finds the image."""
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
    )
    from harbor.trial.trial import Trial

    # Off Linux, Harbor would probe the kernel with a container; the sandbox is Linux.
    monkeypatch.setattr(
        DockerEnvironment, "_egress_control_kernel_support", staticmethod(lambda: True)
    )
    task = _write_task(tmp_path / "task", toml=_NETWORK[shape])
    monkeypatch.setenv("MODEL_TOKEN", "sk-trial-only")  # a trial holds its keys
    trial = await Trial.create(
        TrialConfig(
            task=TaskConfig(path=task),
            trial_name="task__AbCdEfG",
            trials_dir=tmp_path / "trials",
            agent=AgentConfig(name="oracle"),
            environment=EnvironmentConfig(type="docker"),
        )
    )
    restricted = trial.agent_environment._enable_egress_control

    assert restricted is (shape != "public")
    assert await compose_build.build_network_sidecar(task) is restricted
    if restricted:
        [built] = sidecar_builds
        assert (
            built["docker_name"]
            == DockerEnvironment._EGRESS_CONTROL_SIDECAR_DOCKER_NAME
        )
        assert (
            built["docker_build_context"]
            == DockerEnvironment._EGRESS_CONTROL_SIDECAR_CONTEXT_PATH
        )
        assert built["build_args"] == {} and built["platform"] == "linux/amd64"
    else:
        assert sidecar_builds == []


@pytest.mark.asyncio
async def test_warm_builds_the_sidecar_for_a_standard_task_too(
    tmp_path, docker, monkeypatch, sidecar_builds
):
    task = _write_task(
        tmp_path / "src", compose=None, toml=_NETWORK["no_network_baseline"]
    )
    _warm_request(monkeypatch, tmp_path, task)

    assert await warm.run() == 0

    assert len(sidecar_builds) == 1
    # The complete-image path still builds the task image itself.
    assert [call.args[:2] for call in docker.calls] == [("docker", "build")]
