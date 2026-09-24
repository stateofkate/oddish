"""``oddish-mon status``: one JSON line the control plane reads through provider exec.

The running monitor rewrites the status file on every loop tick; this command
only reports it, plus whether that monitor process is still alive.
"""

from __future__ import annotations

import json
import os
import time
from enum import StrEnum
from pathlib import Path

STATUS_PATH = Path(
    os.environ.get("ODDISH_MON_STATUS_PATH", "/run/oddish-mon/status.json")
)


class MonitorPhase(StrEnum):
    """The phases the monitor records itself. Between them, each Harbor hook
    event is recorded under its own value."""

    STARTING = "starting"
    REPORTING = "reporting"
    DONE = "done"  # the control plane accepted the result
    STOPPED = "stopped"  # the control plane ended the attempt (410/401)


# Phases after which the control plane knows how the attempt ended. Once one is
# recorded, a later stage write (a Harbor hook still running) cannot replace it.
TERMINAL_PHASES = (MonitorPhase.DONE, MonitorPhase.STOPPED)


def write_status(*, trial_id: str, phase: str, path: Path | None = None) -> None:
    path = path or STATUS_PATH
    if phase not in TERMINAL_PHASES:
        try:
            recorded = json.loads(path.read_text()).get("phase")
        except (OSError, ValueError, AttributeError):
            recorded = None
        if recorded in TERMINAL_PHASES:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "trial_id": trial_id,
                "phase": phase,
                "updated_at": time.time(),
            }
        )
    )
    tmp.replace(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_status(path: Path = STATUS_PATH) -> dict:
    """``alive`` is about the monitor PROCESS, not about the file existing."""
    try:
        status = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"alive": False, "reason": "no status"}
    status["alive"] = _pid_alive(int(status.get("pid", -1)))
    status["age_sec"] = round(time.time() - float(status.get("updated_at", 0)), 1)
    return status
