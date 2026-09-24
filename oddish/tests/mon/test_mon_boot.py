"""Exercise the real snapshot boot script with stand-ins, without Docker: a
snapshot's layout under a tmp dir, a real bundle served over local HTTP to the
real curl, and a ``python`` that records the failed-start reports."""

from __future__ import annotations

import hashlib
import http.server
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.workers.launcher import code_bundle
from oddish.workers.launcher.providers.base import BUNDLE_SHA256_VAR, BUNDLE_URL_VAR

SCRIPT = Path(__file__).parents[2] / "scripts/mon-boot.sh"
SIGNATURE = "X-Amz-Signature=do-not-log"

# The bundle's entrypoint stands in for the monitor: it records how it was
# started, in which process, and which code it finds, then exits 7.
NEW_ENTRYPOINT = """#!/bin/sh
echo "new-entrypoint $* pid=$$ url=${ODDISH_MON_BUNDLE_URL-unset} \
sha=${ODDISH_MON_BUNDLE_SHA256-unset} code=$(cat "$SNAPSHOT/src/oddish/which")" \
  >> "$PROOF_ROOT/entry-calls"
exit 7
"""


@pytest.fixture
def server():
    """Serves ``routes`` (path -> (status, body)) on 127.0.0.1."""
    routes: dict[str, tuple[int, bytes]] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = routes.get(self.path.split("?")[0], (404, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        routes=routes, url=f"http://127.0.0.1:{httpd.server_address[1]}"
    )
    httpd.shutdown()
    httpd.server_close()


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture
def boot(tmp_path, server):
    """A snapshot's layout, a launcher's bundle, and a way to run the script."""
    snapshot = tmp_path / "opt/oddish"
    _write(snapshot / "src/oddish/which", "snapshot")
    _write(snapshot / "uv.lock", "lock 1\n")
    _write(snapshot / "pyproject.toml", "project 1\n")
    # The snapshot's own interpreter: it only ever reports a failed start here.
    _write(
        snapshot / ".venv/bin/python",
        f"#!{sys.executable}\nimport os, pathlib, sys\n"
        "pathlib.Path(os.environ['PROOF_ROOT'], 'python-calls').open('a')"
        ".write(' '.join(sys.argv[1:]) + '\\n')\n",
        0o755,
    )
    entrypoint = tmp_path / "usr/local/bin/mon-entrypoint.sh"
    _write(
        entrypoint,
        '#!/bin/sh\necho old-entrypoint >> "$PROOF_ROOT/entry-calls"\n',
        0o755,
    )
    (tmp_path / "var/log").mkdir(parents=True)

    launcher = tmp_path / "launcher"  # the launcher's oddish folder
    _write(launcher / "src/oddish/which", "bundle")
    _write(launcher / "uv.lock", "lock 1\n")
    _write(launcher / "pyproject.toml", "project 1\n")
    _write(launcher / "scripts/mon-entrypoint.sh", NEW_ENTRYPOINT)

    paths = {
        "/opt/oddish": snapshot,
        "/usr/local/bin/mon-entrypoint.sh": entrypoint,
        "/var/log/oddish-mon.log": tmp_path / "var/log/oddish-mon.log",
    }
    script = re.sub(
        "|".join(map(re.escape, paths)), lambda m: str(paths[m[0]]), SCRIPT.read_text()
    )

    def run(*, url: str | None = None, sha: str | None = None, bundle=None, path=""):
        data = code_bundle.build(launcher) if bundle is None else bundle
        server.routes["/bundle.tar.gz"] = (200, data)
        env = {
            "PATH": f"{path}/usr/bin:/bin:/usr/sbin:/sbin",
            "PROOF_ROOT": str(tmp_path),
            "SNAPSHOT": str(snapshot),
            BUNDLE_URL_VAR: url or f"{server.url}/bundle.tar.gz?{SIGNATURE}",
            BUNDLE_SHA256_VAR: sha or hashlib.sha256(data).hexdigest(),
        }
        env = {name: value for name, value in env.items() if value != "-"}
        # As Modal runs it: the script's text is the sandbox command.
        process = subprocess.Popen(
            ["sh", "-c", script], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return SimpleNamespace(
            returncode=process.returncode, stdout=stdout, stderr=stderr, pid=process.pid
        )

    def lines(name: str) -> list[str]:
        path = tmp_path / name
        return path.read_text().splitlines() if path.exists() else []

    return SimpleNamespace(
        run=run,
        lines=lines,
        launcher=launcher,
        snapshot=snapshot,
        entrypoint=entrypoint,
        log=lambda: lines("var/log/oddish-mon.log"),
        scratch=lambda: sorted(p.name for p in (tmp_path / "opt").iterdir()),
    )


def _unchanged(boot) -> None:
    """Nothing started and nothing swapped: the snapshot is as it booted."""
    assert (boot.snapshot / "src/oddish/which").read_text() == "snapshot"
    assert "old-entrypoint" in boot.entrypoint.read_text()
    assert boot.lines("entry-calls") == []
    # The snapshot's own code reported the failed start, once.
    assert boot.lines("python-calls") == ["-m oddish.mon startup-failed 1"]
    assert "metric=monitor_boot outcome=failed" in boot.log()[-2]


def test_a_good_bundle_swaps_the_code_and_starts_the_monitor_once(boot):
    result = boot.run()

    assert result.returncode == 7, result.stderr  # the entrypoint's own exit
    # Once, and exec'd: the monitor's entrypoint replaced the boot script's
    # process, so nothing of the script runs after it.
    assert boot.lines("entry-calls") == [
        f"new-entrypoint run pid={result.pid} url=unset sha=unset code=bundle"
    ]
    assert (boot.snapshot / "src/oddish/which").read_text() == "bundle"
    assert boot.entrypoint.read_text() == NEW_ENTRYPOINT
    assert boot.entrypoint.stat().st_mode & 0o777 == 0o755
    assert boot.lines("python-calls") == []  # nothing to report
    assert boot.scratch() == ["oddish"]  # the scratch dir is gone
    sha = hashlib.sha256(code_bundle.build(boot.launcher)).hexdigest()
    (line,) = boot.log()
    assert f"metric=monitor_boot code_source=bundle bundle={sha} fetch_s=" in line
    assert re.search(r"fetch_s=\d+\.\d+$", line)
    assert SIGNATURE not in line and SIGNATURE not in result.stdout


def test_a_bundle_that_is_not_the_one_named_starts_no_monitor(boot):
    result = boot.run(sha="0" * 64)
    assert result.returncode == 1
    _unchanged(boot)
    assert "sha256" in boot.log()[-1] and "no monitor started" in boot.log()[-1]


@pytest.mark.parametrize("file", ["uv.lock", "pyproject.toml"])
def test_a_bundle_locked_unlike_the_snapshot_starts_no_monitor(boot, file):
    """The snapshot's venv came from its own lock; the key keeps them equal, so
    a difference is a bug, and the code never runs against the other venv."""
    (boot.launcher / file).write_text("bumped\n")
    result = boot.run()
    assert result.returncode == 1
    _unchanged(boot)
    assert f"{file} differs" in boot.log()[-1]


@pytest.mark.parametrize("status", [403, 404])
def test_a_bundle_that_cannot_be_fetched_starts_no_monitor(boot, server, status):
    server_url = f"{server.url}/expired.tar.gz?{SIGNATURE}"
    server.routes["/expired.tar.gz"] = (status, b"")
    result = boot.run(url=server_url)
    assert result.returncode == 1
    _unchanged(boot)
    assert "fetch failed" in boot.log()[-1] and str(status) in boot.log()[-1]
    assert SIGNATURE not in "\n".join(boot.log())


def test_a_bundle_that_does_not_extract_starts_no_monitor(boot):
    garbage = b"not a tarball"
    result = boot.run(bundle=garbage)
    assert result.returncode == 1
    _unchanged(boot)
    assert "does not extract" in boot.log()[-1]


@pytest.mark.parametrize("missing", [BUNDLE_URL_VAR, BUNDLE_SHA256_VAR])
def test_no_bundle_named_starts_no_monitor(boot, missing):
    result = boot.run(**{"url" if missing == BUNDLE_URL_VAR else "sha": "-"})
    assert result.returncode == 1
    _unchanged(boot)
    assert "the launcher named no code bundle" in boot.log()[-1]


def test_a_fetch_error_that_names_the_link_never_logs_its_signature(boot, tmp_path):
    """curl's own messages name the host at most; the script cuts any query
    string anyway, since the failed-start report ships the log's tail."""
    fake = tmp_path / "fake-bin"
    _write(
        fake / "curl",
        f'#!/bin/sh\necho "curl: (22) https://bucket.test/b.tar.gz?{SIGNATURE} 403" >&2\n'
        "exit 22\n",
        0o755,
    )
    result = boot.run(path=f"{fake}:")
    assert result.returncode == 1
    _unchanged(boot)
    assert "https://bucket.test/b.tar.gz 403" in boot.log()[-1]
    assert SIGNATURE not in "\n".join(boot.log())


@pytest.mark.parametrize("tool", ["mkdir", "rm"])
def test_a_failed_scratch_step_still_starts_a_monitor_or_reports(boot, tmp_path, tool):
    """Under ``set -e`` an unchecked step would end the script silently: no
    monitor, and no failed-start report either."""
    _write(tmp_path / "fake-bin" / tool, "#!/bin/sh\nexit 1\n", 0o755)
    result = boot.run(path=f"{tmp_path / 'fake-bin'}:")
    if tool == "mkdir":
        assert result.returncode == 1
        _unchanged(boot)
    else:  # only the scratch dir is left behind
        assert result.returncode == 7, result.stderr
        assert (boot.snapshot / "src/oddish/which").read_text() == "bundle"


def test_the_script_reads_the_names_the_launcher_sets():
    script = SCRIPT.read_text()
    assert f"${{{BUNDLE_URL_VAR}:-}}" in script
    assert f"${{{BUNDLE_SHA256_VAR}:-}}" in script
    assert "set -x" not in script  # a traced run would print the signed link
