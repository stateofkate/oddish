"""A failed database cleanup must not keep a closed PR's Vercel links."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/preview/stop_preview.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _run_cleanup(tmp_path, *, neon_status=0, pull_status=0, vercel=True, gke_status=0):
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    preview = scripts / "preview"
    preview.mkdir()
    shutil.copy(SCRIPT, preview / "stop_preview.sh")
    gke = preview / "run_gke_teardown.sh"
    gke.write_text('#!/usr/bin/env bash\necho gke >> "$ORDER"\nexit "$GKE_STATUS"\n')
    gke.chmod(0o755)
    (tmp_path / "frontend").mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "python").symlink_to(sys.executable)
    (scripts / "neon_database.py").write_text(
        "import os, sys\n"
        "with open(os.environ['ORDER'], 'a') as f: f.write('neon ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ['NEON_STATUS']))\n"
    )
    for tool in ("modal", "vercel"):
        path = bins / tool
        path.write_text(
            f'#!/usr/bin/env bash\necho \'{tool} \' "$*" >> "$ORDER"\n'
            + (
                'if [ "$1" = pull ]; then exit "$PULL_STATUS"; fi\n'
                if tool == "vercel"
                else ""
            )
        )
        path.chmod(0o755)
    order = tmp_path / "order"
    result = subprocess.run(
        ["bash", str(preview / "stop_preview.sh")],
        env={
            **os.environ,
            "PATH": f"{bins}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(tmp_path),
            "MODAL_ENVIRONMENT": "preview",
            "MODAL_APP_NAME": "oddish-pr-42",
            "VERCEL_GIT_BRANCH": "feature",
            "VERCEL_TOKEN": "fake-token" if vercel else "",
            "VERCEL_ORG_ID": "fake-org",
            "VERCEL_PROJECT_ID": "fake-project",
            "PREVIEW_ALIAS_HOSTNAME": "pr-42.oddish.app",
            "NEON_STATUS": str(neon_status),
            "PULL_STATUS": str(pull_status),
            "GKE_STATUS": str(gke_status),
            "ORDER": str(order),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, order.read_text()


@pytest.mark.parametrize("neon_status", [0, 1])
def test_vercel_cleanup_runs_even_when_neon_fails(tmp_path, neon_status):
    result, order = _run_cleanup(tmp_path, neon_status=neon_status)
    assert result.returncode == neon_status
    assert "neon delete-preview" in order
    assert "env rm NEXT_PUBLIC_API_URL" in order
    assert "env rm NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL" in order
    assert "alias rm pr-42.oddish.app" in order
    assert order.index("neon delete-preview") < order.index("vercel")


def test_vercel_pull_failure_does_not_skip_alias_cleanup(tmp_path):
    result, order = _run_cleanup(tmp_path, pull_status=1)
    assert result.returncode == 1
    assert "env rm NEXT_PUBLIC_API_URL" in order
    assert "alias rm pr-42.oddish.app" in order


def test_neon_failure_is_reported_when_vercel_is_not_configured(tmp_path):
    result, order = _run_cleanup(tmp_path, neon_status=1, vercel=False)
    assert result.returncode == 1
    assert "vercel" not in order
    assert "Neon preview cleanup failed" in result.stderr


def test_gke_teardown_failure_keeps_the_app_and_its_database(tmp_path):
    result, order = _run_cleanup(tmp_path, gke_status=1)
    assert result.returncode == 1
    assert order.strip() == "gke"
    assert "leaving oddish-pr-42 running" in result.stdout


def _app(state, *, display_keys=False):
    if display_keys:
        return [{"App ID": "ap-example", "Description": "oddish-pr-42", "State": state}]
    return [{"description": "oddish-pr-42", "state": state}]


def _run_reset_stop(tmp_path, listings, *, gke_exit=0, stop_exit=0):
    script = SCRIPT.with_name("stop_modal_preview_app.sh")
    shutil.copy(script, tmp_path / script.name)
    helper = tmp_path / "run_gke_teardown.sh"
    helper.write_text(f'#!/usr/bin/env bash\necho gke >> "$ORDER"\nexit {gke_exit}\n')
    helper.chmod(0o755)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"listings": listings, "stop_exit": stop_exit}))
    modal = tmp_path / "modal"
    modal.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "config = json.loads(Path(os.environ['CONFIG']).read_text())\n"
        "action = sys.argv[2]\n"
        "with open(os.environ['ORDER'], 'a') as f: f.write(action + '\\n')\n"
        "if action == 'list':\n"
        "    counter = Path(os.environ['CONFIG'] + '.count')\n"
        "    number = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(number + 1))\n"
        "    listing = config['listings'][min(number, len(config['listings']) - 1)]\n"
        "    if isinstance(listing, int): sys.exit(listing)\n"
        "    print(listing if isinstance(listing, str) else json.dumps(listing))\n"
        "else:\n"
        "    sys.exit(config['stop_exit'])\n"
    )
    modal.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    order = tmp_path / "order"
    result = subprocess.run(
        ["bash", str(tmp_path / script.name)],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MODAL_ENVIRONMENT": "preview",
            "MODAL_APP_NAME": "oddish-pr-42",
            "CONFIG": str(config),
            "ORDER": str(order),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, order.read_text().splitlines()


@pytest.mark.parametrize("listing", [[], _app("stopped")])
def test_reset_accepts_an_absent_or_already_stopped_app(tmp_path, listing):
    result, order = _run_reset_stop(tmp_path, [listing])
    assert result.returncode == 0, result.stderr
    assert order == ["list"]


@pytest.mark.parametrize(
    "listing", [1, "not JSON", {}, _app("unknown"), [{"state": "deployed"}]]
)
def test_reset_blocks_on_failed_or_invalid_modal_state(tmp_path, listing):
    result, order = _run_reset_stop(tmp_path, [listing])
    assert result.returncode == 1
    assert order == ["list"]
    assert "Database reset is blocked" in result.stderr


@pytest.mark.parametrize("display_keys", [False, True])
def test_reset_stops_cluster_and_app_then_confirms_stopped_state(
    tmp_path, display_keys
):
    result, order = _run_reset_stop(
        tmp_path,
        [_app(state, display_keys=display_keys) for state in ("deployed", "stopped")],
    )
    assert result.returncode == 0, result.stderr
    assert order == ["list", "gke", "stop", "list"]


def test_reset_waits_for_modal_stopping_state(tmp_path):
    result, order = _run_reset_stop(
        tmp_path, [_app("deployed"), _app("stopping..."), _app("stopped")]
    )
    assert result.returncode == 0, result.stderr
    assert order == ["list", "gke", "stop", "list", "list"]


def test_reset_gke_failure_blocks_app_stop(tmp_path):
    result, order = _run_reset_stop(tmp_path, [_app("deployed")], gke_exit=124)
    assert result.returncode == 1
    assert order == ["list", "gke"]


def test_reset_modal_stop_failure_blocks_database_reset(tmp_path):
    result, order = _run_reset_stop(tmp_path, [_app("deployed")], stop_exit=1)
    assert result.returncode == 1
    assert order == ["list", "gke", "stop"]
    assert "Could not stop" in result.stderr


def test_reset_cannot_continue_without_confirming_the_app_stopped(tmp_path):
    result, order = _run_reset_stop(tmp_path, [_app("deployed"), 1])
    assert result.returncode == 1
    assert order == ["list", "gke", "stop", "list"]


def test_reset_stop_confirmation_is_bounded(tmp_path):
    result, order = _run_reset_stop(tmp_path, [_app("deployed")])
    assert result.returncode == 1
    assert order.count("list") == 11
    assert "still running" in result.stderr
