"""The monitor kills the task's containers at teardown instead of waiting.

Harbor stops the task environment right after the verifier with ``docker
compose down`` (``DockerEnvironment.stop``). Docker sends SIGTERM, waits the
container's stop timeout (10 s unless the task's Compose file sets
``stop_grace_period``), then SIGKILLs. Harbor's main service runs ``sh -c
"sleep infinity"`` as PID 1, which ignores SIGTERM, so every monitor trial
waited out the whole grace period: verifier end to trial end was 11.4 s
(median of 59 staging trials, 21-22 Sep).

The sandbox is destroyed once the result is in, and Harbor has every output
on the host before it stops the environment: the verifier has returned, the
log and artifact directories are bind mounts, and ``stop`` fixes their
ownership while the containers still run. A graceful stop buys nothing here.
So while the monitor drives a trial, each ``down`` Harbor's Docker environment
runs gets ``--timeout 0``: Docker sends SIGKILL right after SIGTERM, then
removes as before. No other Compose command changes, and the worker never
enters this. A trial on an ephemeral Harbor variant runs in a child interpreter
and keeps Harbor's own stop.

Harbor has no setting for it: its ``stop`` passes no timeout and Compose reads
none from the environment. A Compose overlay (``extra_docker_compose``) or an
environment class (``import_path``) in the trial config would also change which
network rules the driver applies (``_supports_auto_restricted_agent_network``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from harbor.environments.docker.docker import DockerEnvironment

_NO_GRACE = ["--timeout", "0"]


def without_grace(command: list[str]) -> list[str]:
    """A ``docker compose down`` with ``--timeout 0``; any other command, or a
    ``down`` that already names its timeout, as it is."""
    if command[:1] != ["down"] or any(
        arg in ("-t", "--timeout") or arg.startswith("--timeout=") for arg in command
    ):
        return command
    return [command[0], *_NO_GRACE, *command[1:]]


@contextlib.contextmanager
def no_grace_period() -> Iterator[None]:
    """While open, every ``docker compose down`` Harbor's Docker environment
    runs in this process kills the containers at once. Harbor's own method is
    back in place on exit."""
    original = DockerEnvironment._run_docker_compose_command

    async def run(self, command, *args, **kwargs):
        return await original(self, without_grace(command), *args, **kwargs)

    DockerEnvironment._run_docker_compose_command = run  # type: ignore[method-assign]
    try:
        yield
    finally:
        DockerEnvironment._run_docker_compose_command = original  # type: ignore[method-assign]
