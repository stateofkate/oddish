"""Outer-sandbox memory headroom is explicit and priced, is folded in by each
provider (Daytona up to its per-sandbox maximum), and never changes the task's
own size; the monitor's memory snapshot reads cgroup v2, then v1, then
/proc/meminfo, and names its source, so a reader knows what the numbers mean."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from oddish.mon import memstat
from oddish.workers.queue.monitor_sandbox import SandboxSize


@dataclass
class _Res:
    spec_source: str = "pinned"
    cpu_request: float | None = 8
    cpu_limit: float | None = 8
    mem_request_mb: int | None = 4096
    mem_limit_mb: int | None = 4096
    gpu_type: str | None = None
    gpu_count: int = 0


def test_headroom_is_carried_beside_the_tasks_size():
    size = SandboxSize.from_span_resources(_Res())
    assert size is not None and (size.memory_mb, size.headroom_mb) == (4096, 0)
    size = SandboxSize.from_span_resources(_Res(), memory_headroom_mb=1024)
    assert size is not None and (size.memory_mb, size.headroom_mb, size.total_memory_mb) == (4096, 1024, 5120)


def test_negative_headroom_is_ignored():
    size = SandboxSize.from_span_resources(_Res(), memory_headroom_mb=-500)
    assert size is not None and size.total_memory_mb == 4096


def test_a_separate_verifier_in_the_same_sandbox_sizes_it_too():
    agent = _Res(cpu_request=2, cpu_limit=2, mem_request_mb=2048, mem_limit_mb=2048)
    verifier = _Res(cpu_request=4, cpu_limit=4, mem_request_mb=8192, mem_limit_mb=8192)
    size = SandboxSize.from_span_resources(agent, verifier=verifier)
    assert size is not None and (size.cpu, size.memory_mb) == (4, 8192)
    size = SandboxSize.from_span_resources(agent, verifier=None)
    assert size is not None and (size.cpu, size.memory_mb) == (2, 2048)
    smaller = _Res(cpu_request=1, cpu_limit=1, mem_request_mb=1024, mem_limit_mb=1024)
    size = SandboxSize.from_span_resources(agent, verifier=smaller)
    assert size is not None and (size.cpu, size.memory_mb) == (2, 2048)
    # Only the verifier pins: it alone sizes the sandbox, as it alone sized
    # its own sandbox on the worker path. The GPU stays the agent's.
    unpinned = _Res(spec_source="provider_default")
    size = SandboxSize.from_span_resources(unpinned, verifier=verifier)
    assert size is not None and (size.cpu, size.memory_mb, size.gpu_count) == (4, 8192, 0)
    assert SandboxSize.from_span_resources(unpinned, verifier=_Res(spec_source="unknown")) is None


def test_the_setting_is_the_measured_monitor_overhead():
    from oddish.config import settings

    assert settings.monitor_memory_headroom_mb == 1024


def test_modal_folds_the_headroom_in_and_bills_it():
    from oddish.workers.queue.monitor_sandbox_modal import ModalStarter, _modal_size

    size = SandboxSize.from_span_resources(_Res(), memory_headroom_mb=1024)
    assert _modal_size(size)["memory"] == 5120
    billed = ModalStarter().provisioned(size).billed_as(_Res())
    assert (billed.mem_request_mb, billed.mem_limit_mb) == (5120, 5120)


def test_daytona_folds_the_headroom_in_up_to_its_maximum():
    from oddish.config import settings
    from oddish.workers.queue.monitor_sandbox_daytona import _whole_units

    cap = settings.monitor_daytona_max_memory_gb
    assert _whole_units(SandboxSize(cpu=4, memory_mb=4096, headroom_mb=1024)) == (4, 5)
    # A 16 GB task stays at 16 GB: Daytona refused 17 GB in QA40 tier A.
    assert _whole_units(SandboxSize(cpu=4, memory_mb=cap * 1024, headroom_mb=1024)) == (4, cap)
    assert _whole_units(SandboxSize(cpu=4, memory_mb=cap * 1024 - 512, headroom_mb=1024)) == (4, cap)
    # A task asking more than the maximum is passed through unchanged.
    assert _whole_units(SandboxSize(cpu=4, memory_mb=(cap + 4) * 1024, headroom_mb=1024)) == (4, cap + 4)


MiB = 1024 * 1024


def test_snapshot_reads_cgroup_v2(tmp_path: Path):
    (tmp_path / "memory.current").write_text(str(1210 * MiB))
    (tmp_path / "memory.peak").write_text(str(3980 * MiB))
    (tmp_path / "memory.max").write_text(str(4096 * MiB))
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\n")
    line = memstat.snapshot(tmp_path, meminfo=tmp_path / "none", with_containers=False)
    assert line == "mem[cgroup2] current=1210MiB peak=3980MiB max=4096MiB oom_kill=1"


def test_snapshot_falls_back_to_cgroup_v1(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "memory.usage_in_bytes").write_text(str(900 * MiB))
    (mem / "memory.max_usage_in_bytes").write_text(str(4000 * MiB))
    (mem / "memory.limit_in_bytes").write_text(str(4096 * MiB))
    (mem / "memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    line = memstat.snapshot(tmp_path, meminfo=tmp_path / "none", with_containers=False)
    assert line == "mem[cgroup1] current=900MiB peak=4000MiB max=4096MiB oom_kill=2"


def test_snapshot_v1_reports_no_limit_as_max(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "memory.usage_in_bytes").write_text("1")
    (mem / "memory.limit_in_bytes").write_text(str(9223372036854771712))
    line = memstat.snapshot(tmp_path, meminfo=tmp_path / "none", with_containers=False)
    assert "max=max" in line


def test_snapshot_falls_back_to_meminfo(tmp_path: Path):
    (tmp_path / "meminfo").write_text("MemTotal:        4194304 kB\nMemFree:  100 kB\nMemAvailable:    2097152 kB\n")
    line = memstat.snapshot(tmp_path, meminfo=tmp_path / "meminfo", with_containers=False)
    assert line == "mem[meminfo] current=- peak=- max=4096MiB oom_kill=- available=2048MiB"


def test_snapshot_is_safe_with_nothing_readable(tmp_path: Path):
    line = memstat.snapshot(tmp_path, meminfo=tmp_path / "none", with_containers=False)
    assert line == "mem[meminfo] current=- peak=- max=- oom_kill=- available=-"
