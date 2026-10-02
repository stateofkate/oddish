"""The monitor's Harbor kills the task's containers at teardown, without the
grace period.

Harbor stops the task environment after the verifier with ``docker compose
down``. Its main service runs ``sh -c "sleep infinity"`` as PID 1, which
ignores SIGTERM, so Docker waited out its grace period (10 s) on every monitor
trial before the SIGKILL: verifier end to trial end was 11.4 s (median of 59
staging trials, 21-22 Sep). Docker waits the timeout a stop names, else the
grace period, so the commands the fake daemon records prove the kill.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from harbor.environments.docker.docker import DockerEnvironment
from harbor.environments.factory import EnvironmentFactory
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from oddish.mon import compose_build, env, runner, teardown
from oddish.mon.exit_codes import EXIT_ACCEPTED

HARBORS_OWN = DockerEnvironment._run_docker_compose_command

_COMPOSE_GLOBALS = ("--project-name", "--project-directory", "-f")
_DELETE = ("down", "--rmi", "local", "--volumes", "--remove-orphans")


def _compose_command(args: tuple[str, ...]) -> tuple[str, ...] | None:
    """The Compose subcommand and its arguments, without Harbor's globals."""
    if args[:2] != ("docker", "compose"):
        return None
    rest = list(args[2:])
    while rest and rest[0] in _COMPOSE_GLOBALS:
        del rest[:2]
    return tuple(rest)


class _Daemon:
    """``docker`` as Harbor runs it: records each Compose command."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def take(self) -> list[tuple[str, ...]]:
        commands, self.commands = self.commands, []
        return commands

    async def __call__(self, *args, **kwargs):
        command = _compose_command(args)
        if command is not None:
            self.commands.append(command)

        class _Process:
            returncode = 0
            stdin = None

            async def communicate(self, input=None):
                return b"", b""

            async def wait(self):
                return 0

        return _Process()


@pytest.fixture
def daemon(monkeypatch):
    fake = _Daemon()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return fake


def _environment(root: Path) -> DockerEnvironment:
    """A trial's Docker environment as Harbor's factory makes it, with the log
    mounts the warm-up build gives it too."""
    environment_dir = root / "task" / "environment"
    environment_dir.mkdir(parents=True)
    (environment_dir / "Dockerfile").write_text("FROM debian:bookworm-slim\n")
    paths = TrialPaths(root / "trial")
    paths.mkdir()
    inside = EnvironmentPaths.for_os(EnvironmentConfig().os)
    environment = EnvironmentFactory.create_environment_from_config(
        config=TrialEnvironmentConfig(type=EnvironmentType.DOCKER),
        environment_dir=environment_dir,
        environment_name="task",
        session_id="task__trial1",
        trial_paths=paths,
        task_env_config=EnvironmentConfig(),
        mounts=compose_build._log_mounts(paths, inside),
    )
    assert type(environment) is DockerEnvironment
    return environment


async def _stop(environment: DockerEnvironment) -> None:
    """``Trial._stop_agent_environment`` after the verifier (delete defaults True)."""
    await environment.stop(delete=TrialEnvironmentConfig().delete)


@pytest.mark.asyncio
async def test_the_monitor_kills_the_containers_instead_of_waiting(tmp_path, daemon):
    await _stop(_environment(tmp_path / "harbor"))
    harbor = daemon.take()
    with teardown.no_grace_period():
        await _stop(_environment(tmp_path / "monitor"))
    monitor = daemon.take()

    # Harbor alone: SIGTERM, the grace period, SIGKILL.
    assert harbor[-1] == _DELETE
    assert monitor[-1] == ("down", "--timeout", "0", *_DELETE[1:])
    # Everything before the down is Harbor's own: the log mounts' ownership is
    # fixed while the containers still run.
    assert monitor[:-1] == harbor[:-1]
    assert [command[0] for command in monitor[:-1]] == ["exec"] * 3
    assert all("chown" in command[-1] for command in monitor[:-1])


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (list(_DELETE), ["down", "--timeout", "0", *_DELETE[1:]]),  # delete=True
        (["down"], ["down", "--timeout", "0"]),  # delete=False
        # start() clears a stale project first.
        (["down", "--remove-orphans"], ["down", "--timeout", "0", "--remove-orphans"]),
    ],
)
def test_every_down_gets_a_zero_timeout(command, expected):
    assert teardown.without_grace(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # Separate-verifier mode stops the main service mid-trial, before the
        # sidecars' evidence is pulled: that stop keeps its grace period.
        ["stop", "main"],
        ["stop"],
        ["up", "--detach", "--wait"],
        ["build"],
        ["exec", "-u", "root", "main", "bash", "-c", "down"],
        # A down that names its own timeout keeps it.
        ["down", "-t", "5"],
        ["down", "--timeout", "5"],
        ["down", "--timeout=5"],
    ],
)
def test_other_commands_pass_unchanged(command):
    assert teardown.without_grace(command) == command


def test_harbors_own_method_is_back_after_the_drive_even_when_it_raises():
    with (
        pytest.raises(RuntimeError, match="driver crashed"),
        teardown.no_grace_period(),
    ):
        assert DockerEnvironment._run_docker_compose_command is not HARBORS_OWN
        raise RuntimeError("driver crashed")
    assert DockerEnvironment._run_docker_compose_command is HARBORS_OWN


class _Client:
    closed = False

    def __init__(self, spec):
        self.spec = spec

    async def attempt(self):
        return self.spec

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_run_drives_the_trial_without_the_grace_period(
    monkeypatch, tmp_path, daemon, make_spec
):
    client = _Client(make_spec())

    async def prepare(c, spec, monitor_env):
        return spec

    async def drive(c, task, monitor_env):
        # What Harbor runs after the verifier, on the trial's environment.
        await _stop(_environment(tmp_path))
        return "result"

    async def report(c, task, result):
        # Reporting comes after the drive: Harbor's own method is back.
        assert DockerEnvironment._run_docker_compose_command is HARBORS_OWN

    monkeypatch.setattr(
        runner.env,
        "load",
        lambda: env.MonitorEnv(
            api_url="https://api.test", token="t", agent_env={}, registry_auth=[]
        ),
    )
    monkeypatch.setattr(runner, "MonitorClient", lambda monitor_env: client)
    monkeypatch.setattr(runner, "prepare_task", prepare)
    monkeypatch.setattr(runner, "drive_with_watchers", drive)
    monkeypatch.setattr(runner, "report_until_accepted", report)

    assert await runner.run(runner.env.load()) == EXIT_ACCEPTED

    assert daemon.take()[-1] == ("down", "--timeout", "0", *_DELETE[1:])
    assert client.closed
