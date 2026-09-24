"""The monitor's token and heartbeat against a real, throwaway Postgres."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_off_modal_portability import _docker_available, _wait_for_postgres

_PROOFS = [
    "_monitor_heartbeat_proof.py",
    "_monitor_events_proof.py",
    "_monitor_result_proof.py",
    "_monitor_lifecycle_proof.py",
    "_monitor_attempt_proof.py",
    "_monitor_boundary_proof.py",
    "_monitor_retry_proof.py",
    "_monitor_budget_proof.py",
    "_monitor_reaper_fixes_proof.py",
]


@pytest.mark.skipif(not _docker_available(), reason="requires a running Docker daemon")
@pytest.mark.parametrize("proof_name", _PROOFS)
def test_monitor_paths_against_postgres(proof_name: str) -> None:
    container = f"oddish-monitor-pg-{os.getpid()}"
    subprocess.run(["docker", "rm", "-fv", container], capture_output=True)
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container,
            "-e", "POSTGRES_USER=oddish",
            "-e", "POSTGRES_PASSWORD=oddish",
            "-e", "POSTGRES_DB=oddish",
            "-p", "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    try:
        _wait_for_postgres(container)
        port_out = subprocess.run(
            ["docker", "port", container, "5432/tcp"], capture_output=True, text=True
        ).stdout.strip()
        host_port = port_out.splitlines()[0].rsplit(":", 1)[-1]
        env = dict(os.environ)
        env["ODDISH_DATABASE_URL"] = (
            f"postgresql+asyncpg://oddish:oddish@127.0.0.1:{host_port}/oddish"
        )
        proof = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "proofs" / proof_name)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert (
            proof.returncode == 0
        ), f"proof failed\nstdout:\n{proof.stdout}\nstderr:\n{proof.stderr}"
        failed = [
            line
            for line in proof.stdout.splitlines()
            if line.startswith("CHECK ") and line.endswith("=False")
        ]
        assert not failed, proof.stdout
    finally:
        subprocess.run(["docker", "rm", "-fv", container], capture_output=True)
