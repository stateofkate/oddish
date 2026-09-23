"""Exercise the real startup shell with stand-ins, without a local Docker daemon."""

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("warm_exit", "dockerd_exit"),
    [(0, 0), (7, 0), (0, 143)],
    ids=["warmed", "warm-failed", "dockerd-exits-non-zero-on-stop"],
)
def test_warm_boot_ignores_restored_runtime_and_waits_for_flush(
    tmp_path, warm_exit, dockerd_exit
):
    script = (Path(__file__).parents[2] / "scripts/mon-entrypoint.sh").read_text()
    # Relocate OS paths so this test cannot touch the host's cgroups or daemon.
    paths = {
        "/sys/fs/cgroup": tmp_path / "absent-cgroup",
        "/var/log": tmp_path / "log",
        "/var/run": tmp_path / "run",
        "/run": tmp_path / "run",
    }
    script = re.sub("|".join(map(re.escape, paths)), lambda m: str(paths[m[0]]), script)
    (tmp_path / "log").mkdir()
    (tmp_path / "run").mkdir()
    # An unrelated live PID in restored state must not inhibit a fresh daemon.
    stale = tmp_path / "run/docker/containerd/containerd.pid"
    stale.parent.mkdir(parents=True)
    stale.write_text(str(os.getpid()))
    entry = tmp_path / "entry.sh"
    entry.write_text(script)
    binaries = tmp_path / "bin"
    binaries.mkdir()

    def executable(name, body):
        path = binaries / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    executable("dockerd", """
import os, pathlib, signal, sys, time
root = pathlib.Path(os.environ['PROOF_ROOT'])
args = dict(a.split('=', 1) for a in sys.argv[1:])
runtime = pathlib.Path(args.get('--exec-root', str(root / 'run/docker')))
if (runtime / 'containerd/containerd.pid').exists():
    sys.exit(1)
(root / 'runtime-paths').open('a').write(str(runtime) + '\\n')
def stop(*_):
    time.sleep(0.15)
    (root / 'flushed').write_text('yes')
    sys.exit(int(os.environ.get('DOCKERD_STOP_EXIT', '0')))
signal.signal(signal.SIGTERM, stop)
(root / 'ready').touch()
while True:
    time.sleep(0.01)
""")
    executable("docker", """
import os, pathlib, sys
sys.exit(0 if (pathlib.Path(os.environ['PROOF_ROOT']) / 'ready').exists() else 1)
""")
    executable("python", "import os, sys\nsys.exit(int(os.environ['WARM_EXIT']))\n")
    env = {
        **os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PROOF_ROOT": str(tmp_path), "WARM_EXIT": str(warm_exit),
        "DOCKERD_STOP_EXIT": str(dockerd_exit),
    }
    for _ in range(2):
        result = subprocess.run(["sh", str(entry), "warm"], env=env, timeout=10)
        # The warm result is the exit code, whatever dockerd said on SIGTERM.
        assert result.returncode == warm_exit
        assert (tmp_path / "flushed").read_text() == "yes"
        # The runtime dir is removed before the snapshot even then.
        runtime = Path((tmp_path / "runtime-paths").read_text().splitlines()[-1])
        assert not runtime.exists()
        (tmp_path / "flushed").unlink()
        (tmp_path / "ready").unlink()
    runtimes = (tmp_path / "runtime-paths").read_text().splitlines()
    assert len(set(runtimes)) == 2
    assert stale.read_text() == str(os.getpid())
def _entry(tmp_path, *, docker_ready: bool, run_exit: int):
    """The real script with OS paths relocated, a dockerd stand-in that is or
    is never ready, and a ``python`` that records every invocation."""
    script = (Path(__file__).parents[2] / "scripts/mon-entrypoint.sh").read_text()
    paths = {
        "/sys/fs/cgroup": tmp_path / "absent-cgroup",
        "/var/log": tmp_path / "log",
        "/var/run": tmp_path / "run",
        "/run": tmp_path / "run",
    }
    script = re.sub("|".join(map(re.escape, paths)), lambda m: str(paths[m[0]]), script)
    script = script.replace("sleep 1\n", "sleep 0.01\n")  # the 60-tick wait, fast
    (tmp_path / "log").mkdir()
    (tmp_path / "run").mkdir()
    entry = tmp_path / "entry.sh"
    entry.write_text(script)
    binaries = tmp_path / "bin"
    binaries.mkdir()

    def executable(name, body):
        path = binaries / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    executable("dockerd", "import time\nwhile True:\n    time.sleep(0.01)\n")
    executable("docker", f"import sys\nsys.exit({0 if docker_ready else 1})\n")
    executable(
        "python",
        """
import os, pathlib, sys
pathlib.Path(os.environ['PROOF_ROOT'], 'python-calls').open('a').write(' '.join(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ['RUN_EXIT']) if sys.argv[-1] == 'run' else 0)
""",
    )
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PROOF_ROOT": str(tmp_path),
        "RUN_EXIT": str(run_exit),
    }
    return entry, env


def _python_calls(tmp_path):
    return (tmp_path / "python-calls").read_text().splitlines()


def test_a_monitor_that_dies_reports_the_failed_start_and_keeps_its_exit_code(tmp_path):
    entry, env = _entry(tmp_path, docker_ready=True, run_exit=5)
    result = subprocess.run(["sh", str(entry)], env=env, timeout=10)
    assert result.returncode == 5
    assert _python_calls(tmp_path) == [
        "-m oddish.mon run",
        "-m oddish.mon startup-failed 5",
    ]


def test_a_monitor_that_exits_cleanly_reports_nothing(tmp_path):
    entry, env = _entry(tmp_path, docker_ready=True, run_exit=0)
    result = subprocess.run(["sh", str(entry), "run"], env=env, timeout=10)
    assert result.returncode == 0
    assert _python_calls(tmp_path) == ["-m oddish.mon run"]


def test_a_monitor_stopped_by_the_control_plane_reports_nothing(tmp_path):
    from oddish.mon.exit_codes import EXIT_STOPPED

    entry, env = _entry(tmp_path, docker_ready=True, run_exit=EXIT_STOPPED)
    result = subprocess.run(["sh", str(entry), "run"], env=env, timeout=10)
    assert result.returncode == EXIT_STOPPED
    assert _python_calls(tmp_path) == ["-m oddish.mon run"]


def test_a_docker_daemon_that_never_comes_up_is_reported_as_a_failed_start(tmp_path):
    entry, env = _entry(tmp_path, docker_ready=False, run_exit=0)
    result = subprocess.run(
        ["sh", str(entry)], env=env, timeout=30, capture_output=True
    )
    assert result.returncode == 1
    assert b"dockerd did not start" in result.stderr
    assert _python_calls(tmp_path) == ["-m oddish.mon startup-failed 1"]


def test_a_warm_up_sandbox_never_reports(tmp_path):
    # No credential in a warm-up sandbox: a failed warm exits with its code, quietly.
    entry, env = _entry(tmp_path, docker_ready=False, run_exit=0)
    result = subprocess.run(
        ["sh", str(entry), "warm"], env=env, timeout=30, capture_output=True
    )
    assert result.returncode == 1
    assert not (tmp_path / "python-calls").exists()


@pytest.mark.parametrize(
    ("phase", "run_exit", "reported"),
    [
        ("stopped", 1, False),  # killed after our API ended the attempt
        ("done", 137, False),  # killed after the result was accepted
        ("reporting", 137, True),  # killed before the result was accepted
        (None, 3, False),  # unreadable status: the exit code decides
        (None, 1, True),
    ],
)
def test_the_status_file_says_whether_the_control_plane_knows_the_end(
    tmp_path, phase, run_exit, reported
):
    from oddish.mon.status import write_status

    entry, env = _entry(tmp_path, docker_ready=True, run_exit=run_exit)
    status_path = tmp_path / "run/oddish-mon/status.json"
    if phase is None:
        status_path.parent.mkdir(parents=True)
        status_path.write_text("{not json")
    else:
        write_status(trial_id="trial-1", phase=phase, path=status_path)
    result = subprocess.run(["sh", str(entry), "run"], env=env, timeout=10, check=False)
    assert result.returncode == run_exit
    expected = ["-m oddish.mon run"]
    if reported:
        expected.append(f"-m oddish.mon startup-failed {run_exit}")
    assert _python_calls(tmp_path) == expected
