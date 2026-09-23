"""One-line memory snapshot of the sandbox, for the monitor log.

Evidence for sizing the outer sandbox: what the sandbox allows and has peaked
at, whether it has recorded an out-of-memory kill, and the memory limit Docker
gave each running task container. Read-only, best effort, cheap.

The sandbox's own cgroup is read from cgroup v2 (``/sys/fs/cgroup``), else
cgroup v1 (``/sys/fs/cgroup/memory``), else ``/proc/meminfo`` for the machine
view (total and available, no peak). Whichever answered is named in the line so
a reader knows what the numbers mean. Missing files read as ``-``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return "-"


def _mib(value: str) -> str:
    if value in ("-", "max"):
        return value
    try:
        n = int(value)
    except ValueError:
        return value
    # cgroup v1 reports "no limit" as a huge number.
    if n >= 1 << 60:
        return "max"
    return f"{n // (1024 * 1024)}MiB"


def _v2(root: Path) -> dict[str, str] | None:
    if not (root / "memory.current").exists():
        return None
    events = _read(root / "memory.events")
    oom = next((l.split()[1] for l in events.splitlines() if l.startswith("oom_kill ")), "-")
    return {
        "src": "cgroup2",
        "current": _mib(_read(root / "memory.current")),
        "peak": _mib(_read(root / "memory.peak")),
        "max": _mib(_read(root / "memory.max")),
        "oom_kill": oom,
    }


def _v1(root: Path) -> dict[str, str] | None:
    mem = root / "memory"
    if not (mem / "memory.usage_in_bytes").exists():
        return None
    ctl = _read(mem / "memory.oom_control")
    oom = next((l.split()[1] for l in ctl.splitlines() if l.startswith("oom_kill ")), "-")
    return {
        "src": "cgroup1",
        "current": _mib(_read(mem / "memory.usage_in_bytes")),
        "peak": _mib(_read(mem / "memory.max_usage_in_bytes")),
        "max": _mib(_read(mem / "memory.limit_in_bytes")),
        "oom_kill": oom,
    }


def _meminfo(path: Path) -> dict[str, str]:
    text = _read(path)
    vals: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            vals[parts[0][:-1]] = f"{int(parts[1]) // 1024}MiB"
    return {
        "src": "meminfo",
        "current": "-",
        "peak": "-",
        "max": vals.get("MemTotal", "-"),
        "oom_kill": "-",
        "available": vals.get("MemAvailable", "-"),
    }


def container_limits(timeout: float = 5.0) -> str:
    """``name=limit`` for each running container; ``-`` if Docker is not up."""
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=timeout
        ).stdout.split()
        if not ids:
            return "-"
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.Name}}={{.HostConfig.Memory}}", *ids],
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout.split()
        return ",".join(
            f"{n.lstrip('/')}={_mib(v) if v != '0' else 'unlimited'}"
            for n, v in (line.split("=", 1) for line in out)
        )
    except (OSError, subprocess.SubprocessError):
        return "-"


def snapshot(
    root: Path = CGROUP, *, meminfo: Path = MEMINFO, with_containers: bool = True
) -> str:
    """e.g. ``mem[cgroup2] current=1210MiB peak=3980MiB max=4096MiB oom_kill=0 containers=main=4096MiB``"""
    data = _v2(root) or _v1(root) or _meminfo(meminfo)
    parts = [f"mem[{data['src']}]"] + [
        f"{k}={data[k]}" for k in ("current", "peak", "max", "oom_kill") if k in data
    ]
    if "available" in data:
        parts.append(f"available={data['available']}")
    if with_containers:
        parts.append(f"containers={container_limits()}")
    return " ".join(parts)
