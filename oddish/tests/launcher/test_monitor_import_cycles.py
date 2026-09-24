"""The reconciler (``workers.queue.cleanup``) imports the monitor reaper, and
the launcher imports queue modules. Each imports first, in a fresh interpreter,
so a cycle between them cannot come back unnoticed."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "oddish.workers.launcher.reaper",
        "oddish.workers.launcher.launch",
        "oddish.workers.queue",
        "oddish.workers.queue.cleanup",
        "oddish.workers.queue.pending_teardowns",
    ],
)
def test_it_imports_first_without_a_cycle(module):
    run = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
