"""Vertex AI credentials live in their own Modal secret, mounted like the runtime one."""

import json
import os
import subprocess
import sys
from pathlib import Path

import modal_app
import modal_runtime
import worker.functions as worker_functions


def test_vertex_secret_is_dedicated_and_broadly_mounted() -> None:
    assert modal_app.VERTEX_SECRET_NAME == "oddish-vertex"
    assert modal_app.vertex_secret is not modal_runtime.runtime_secret
    assert modal_app.vertex_secret in modal_app.runtime_secrets
    assert modal_app.VERTEX_SECRET_NAME in modal_app._broad_runtime_secret_names


def test_vertex_secret_reaches_api_dispatcher_and_workers() -> None:
    assert modal_app.vertex_secret in worker_functions.trial_worker_secrets
    assert modal_app.vertex_secret in worker_functions.ec2_trial_worker_secrets
    assert modal_app.vertex_secret in worker_functions.thunder_trial_worker_secrets
    assert modal_app.vertex_secret in worker_functions.reconciler_secrets


def test_vertex_secret_can_be_skipped_for_deploys_without_one() -> None:
    """A deploy that keeps VERTEX_AI_* in the runtime secret references no extra secret."""
    code = """
import json
import modal_app
print(json.dumps({
    "name": modal_app.VERTEX_SECRET_NAME,
    "mounted": modal_app.vertex_secret is not None,
    "broad": "oddish-vertex" in modal_app._broad_runtime_secret_names,
}))
"""
    env = {
        **os.environ,
        "ODDISH_VERTEX_SECRET_NAME": "",
        "ODDISH_SAURON_AWS_SECRET_NAME": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(modal_app.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "name": "",
        "mounted": False,
        "broad": False,
    }
