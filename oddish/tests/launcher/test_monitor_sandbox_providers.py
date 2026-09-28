"""Provider routing and the Daytona starter. No provider SDK call leaves this process."""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from oddish.config import settings
from oddish.workers.jobs import handlers as handlers_module
from oddish.workers.launcher.providers import base as monitor_sandbox
from oddish.workers.launcher.providers import daytona as daytona_starter
from oddish.workers.launcher.providers.base import SandboxSize


@pytest.fixture(autouse=True)
def _no_forced_provider(monkeypatch):
    monkeypatch.setattr(settings, "monitor_sandbox_starter", None)
    monkeypatch.setattr(settings, "harbor_environment", "daytona")


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("modal", monitor_sandbox.SandboxProvider.MODAL),
        ("daytona", monitor_sandbox.SandboxProvider.DAYTONA),
        ("Daytona", monitor_sandbox.SandboxProvider.DAYTONA),
        (None, monitor_sandbox.SandboxProvider.DAYTONA),  # the deployment default
        ("gke", None),  # no starter yet: stays on the worker
        ("ec2", None),
        ("docker", None),
    ],
)
def test_a_trial_runs_on_the_provider_it_asked_for(environment, expected):
    assert monitor_sandbox.provider_for_environment(environment) == expected


def test_a_forced_provider_wins(monkeypatch):
    monkeypatch.setattr(settings, "monitor_sandbox_starter", "modal")
    assert monitor_sandbox.provider_for_environment("daytona") == "modal"


def test_every_registered_provider_loads_and_names_itself():
    for provider in monitor_sandbox.SandboxProvider:
        starter = monitor_sandbox.get_starter(provider)
        assert starter.provider is provider
        # The job row stores the plain value; looking it up by that works too.
        assert monitor_sandbox.get_starter(provider.value).provider is provider
        assert all(
            hasattr(starter, method)
            for method in ("start", "stop", "status", "sandbox_state")
        )


def test_an_unknown_provider_is_refused():
    with pytest.raises(monitor_sandbox.MonitorSandboxError):
        monitor_sandbox.get_starter("nope")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "drives"), [("modal", True), ("daytona", False), ("gke", False)]
)
async def test_the_gate_keeps_unsupported_environments_on_the_worker(
    monkeypatch, environment, drives
):
    trial = SimpleNamespace(kind="agent", harbor_config={}, environment=environment)

    class _Session:
        async def get(self, _model, _id):
            return trial

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    monkeypatch.setattr(handlers_module, "get_session", _get_session)
    monkeypatch.setattr(settings, "monitor_api_url", "https://api.test")
    assert await handlers_module._monitor_can_drive("trial-1") is drives


# --- Daytona starter against a fake SDK -------------------------------------


class _Process:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    warm_exit_code = 0
    warm_output = ""

    async def exec(self, command, timeout=None):
        self._sandbox.commands.append(command)
        if command.endswith(" warm"):
            return SimpleNamespace(
                exit_code=self.warm_exit_code, result=self.warm_output
            )
        return SimpleNamespace(exit_code=0, result='{"alive": true}')


class _Sandbox:
    def __init__(self, params=None, state="started"):
        self.id, self.state, self.params, self.commands = "dt-123", state, params, []
        self.process = _Process(self)
        self.snapshots: list[str] = []
        self.cpu, self.memory = 0, 0

    async def _experimental_create_snapshot(self, name, timeout=None):
        self.snapshots.append(name)


class _Snapshots:
    def __init__(self):
        self.names, self.deleted = set(), []

    async def get(self, name):
        if name not in self.names:
            raise LookupError("no such snapshot")
        return SimpleNamespace(name=name)

    async def delete(self, snapshot):
        self.names.discard(snapshot.name)
        self.deleted.append(snapshot.name)


class _FakeDaytona:
    def __init__(self):
        self.created, self.deleted, self.sandbox = [], [], None
        self.snapshot = _Snapshots()

    boot_size = (0, 0)

    async def create(self, params, timeout=None):
        self.sandbox = _Sandbox(params)
        self.sandbox.cpu, self.sandbox.memory = self.boot_size
        self.created.append((params, timeout))
        return self.sandbox

    async def get(self, external_id):
        if self.sandbox is None or external_id != self.sandbox.id:
            raise LookupError("no such sandbox")
        return self.sandbox

    async def delete(self, sandbox):
        self.deleted.append(sandbox.id)


@pytest.fixture
def fake_daytona(monkeypatch):
    fake = _FakeDaytona()

    @asynccontextmanager
    async def _client():
        yield fake

    monkeypatch.setattr(daytona_starter, "_client", _client)
    monkeypatch.setattr(daytona_starter, "_not_found_errors", lambda: (LookupError,))
    sdk = types.ModuleType("daytona")
    sdk.CreateSandboxFromImageParams = lambda **kwargs: SimpleNamespace(**kwargs)
    sdk.CreateSandboxFromSnapshotParams = lambda **kwargs: SimpleNamespace(**kwargs)
    sdk.Resources = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "daytona", sdk)
    return fake


def test_the_image_never_starts_the_monitor_itself():
    """Providers differ in whether they run an image's ENTRYPOINT, and a
    snapshot boot may differ again. The starter is the only place it starts."""
    dockerfile = (Path(__file__).resolve().parents[2] / "mon.Dockerfile").read_text()
    instructions = [line.split()[0] for line in dockerfile.splitlines() if line.strip()]
    assert "ENTRYPOINT" not in instructions and "CMD" not in instructions


@pytest.mark.asyncio
async def test_status_asks_the_monitor_and_reports_a_gone_sandbox(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.sandbox = _Sandbox()

    reply = await starter.status("dt-123")
    assert reply.alive and reply.output == '{"alive": true}'
    fake_daytona.sandbox.state = "stopped"
    assert await starter.status("dt-123") == monitor_sandbox.StatusReply.gone()
    assert not (await starter.status("dt-missing")).alive


@pytest.mark.asyncio
async def test_status_reports_an_exec_failure_as_no_verdict(fake_daytona, monkeypatch):
    """Seen in review: only the lookup was wrapped; an exec error left the
    sandbox reached raw, unlike Modal's status. The reaper must read either as
    UNKNOWN, never as gone."""
    from oddish.workers.launcher import reaper

    starter = daytona_starter.DaytonaStarter()
    fake_daytona.sandbox = _Sandbox()

    async def _exec_refused(self, command, timeout=None):
        raise RuntimeError("exec refused")

    monkeypatch.setattr(_Process, "exec", _exec_refused)
    with pytest.raises(monitor_sandbox.MonitorSandboxError, match="exec refused"):
        await starter.status("dt-123")
    monkeypatch.setattr(monitor_sandbox, "get_starter", lambda provider: starter)
    assert await reaper.observe("daytona", "dt-123") is reaper.Verdict.UNKNOWN

    async def _lookup_failed(external_id):
        raise ConnectionError("daytona down")

    monkeypatch.setattr(fake_daytona, "get", _lookup_failed)
    with pytest.raises(monitor_sandbox.MonitorSandboxError, match="daytona down"):
        await starter.status("dt-123")
    assert await reaper.observe("daytona", "dt-123") is reaper.Verdict.UNKNOWN


@pytest.mark.asyncio
async def test_a_dropped_warm_snapshot_is_deleted_by_name(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.snapshot.names.add("oddish-mon-warm-abc")

    await starter.delete_warm_image("snapshot:oddish-mon-warm-abc")
    await starter.delete_warm_image("snapshot:oddish-mon-warm-abc")  # already gone
    await starter.delete_warm_image("/oddish/mon.Dockerfile")  # not a snapshot
    assert fake_daytona.snapshot.deleted == ["oddish-mon-warm-abc"]


@pytest.mark.asyncio
async def test_stop_deletes_one_sandbox_by_id_and_tolerates_a_missing_one(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.sandbox = _Sandbox()

    await starter.stop("dt-123")
    await starter.stop("dt-missing")
    assert fake_daytona.deleted == ["dt-123"]


def test_a_provider_formats_as_its_plain_value():
    """It ends up in cache keys, storage paths, log lines and worker_jobs.provider."""
    provider = monitor_sandbox.SandboxProvider.MODAL
    assert f"{provider}" == str(provider) == provider.value == "modal"


# --- the sandbox is sized like the task -------------------------------------


def _span(**overrides):
    base = {
        "cpu_request": 1.0, "cpu_limit": 4.0, "mem_request_mb": 1024, "mem_limit_mb": 16384,
        "gpu_type": None, "gpu_count": 0, "spec_source": "pinned",
    }  # fmt: skip
    return SimpleNamespace(**{**base, **overrides})


def test_size_is_the_tasks_limit_with_no_headroom():
    size = SandboxSize.from_span_resources(_span())
    assert (size.cpu, size.memory_mb, size.gpu_count) == (4.0, 16384, 0)


def test_a_request_only_task_is_sized_by_its_request():
    size = SandboxSize.from_span_resources(_span(cpu_limit=None, mem_limit_mb=None))
    assert (size.cpu, size.memory_mb) == (1.0, 1024)


@pytest.mark.parametrize("source", ["provider_default", "unknown"])
def test_a_task_that_pins_nothing_keeps_the_provider_default(source):
    assert SandboxSize.from_span_resources(_span(spec_source=source)) is None
    assert SandboxSize.from_span_resources(None) is None


def test_modal_size_carries_cpu_memory_and_gpu():
    from oddish.workers.launcher.providers.modal import _modal_size

    assert _modal_size(None) == {"cpu": 2.0, "memory": 4096}  # the shared floor
    assert _modal_size(SandboxSize(cpu=4, memory_mb=16384)) == {
        "cpu": 4.0, "memory": 16384,
    }  # fmt: skip
    gpu = SandboxSize(cpu=8, memory_mb=65536, gpu_type="H100", gpu_count=2)
    assert _modal_size(gpu)["gpu"] == "H100!:2"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["modal", "daytona"])
async def test_the_launcher_reads_the_size_from_the_tasks_own_toml(
    tmp_path, monkeypatch, provider
):
    from oddish.workers.launcher import launch as monitor_launch

    (tmp_path / "task.toml").write_text(
        'version = "1.0"\n[environment]\ncpus = 4\nmemory_mb = 16384\n'
    )

    async def _resolve(**_):
        return tmp_path, None, None

    monkeypatch.setattr(monitor_launch, "resolve_task_directory", _resolve)

    async def _no_judge_costs(**_):
        return None

    monkeypatch.setattr(monitor_launch, "begin_judge_costs_if_enabled", _no_judge_costs)
    prepared = SimpleNamespace(
        task_id="t", task_s3_key=None, task_path=str(tmp_path), trial_harbor_config={}
    )
    job = SimpleNamespace(id="wj-1", subject_id="trial-1", worker_id="w-1")
    resources, verifier_resources, judge_env = await monitor_launch._task_facts(
        job, prepared, provider
    )
    size = SandboxSize.from_span_resources(resources)
    assert (size.cpu, size.memory_mb) == (4.0, 16384)
    assert judge_env == {}  # no ${VAR} in its env blocks: nothing is forwarded
    assert verifier_resources is None  # this task has no separate verifier


# --- Daytona warm snapshots ---------------------------------------------------


# --- pricing what is really provisioned ----------------------------------------


def test_the_sandbox_is_exactly_the_size_the_task_asked_for():
    # Daytona caps a sandbox at 16 GB: a 16 GB task must ask for 16, not more.
    size = SandboxSize.from_span_resources(_span())
    assert (size.cpu, size.memory_mb) == (4.0, 16384)


def test_the_cost_span_records_the_outer_sandbox_not_the_tasks_request():
    from decimal import Decimal

    from oddish.costs.modal_cost import SpanResources
    from oddish.workers.launcher.providers.modal import ModalStarter

    asked = SpanResources(
        cpu_request=1.0, cpu_limit=3.5, mem_request_mb=1024, mem_limit_mb=15000,
        gpu_type=None, gpu_count=0, price_multiplier=Decimal(1),
        container_class="sandbox", spec_source="pinned",
    )  # fmt: skip
    size = SandboxSize.from_span_resources(asked)

    on_modal = ModalStarter().provisioned(size).billed_as(asked)
    assert (on_modal.cpu_limit, on_modal.mem_limit_mb) == (3.5, 15000)

    # Daytona rounds up to whole vCPUs and GiB, and that is what is billed.
    on_daytona = daytona_starter.DaytonaStarter().provisioned(size).billed_as(asked)
    assert (on_daytona.cpu_limit, on_daytona.mem_limit_mb) == (4.0, 15 * 1024)
    assert on_daytona.spec_source == "pinned"  # everything else is untouched


def _fake_modal(monkeypatch, *, poll=None, not_found=False, error=None):
    class NotFoundError(Exception):
        pass

    async def _poll():
        return poll

    async def _from_id(_external_id):
        if not_found:
            raise NotFoundError("no such sandbox")
        if error is not None:
            raise error
        return SimpleNamespace(poll=SimpleNamespace(aio=_poll))

    sdk = types.ModuleType("modal")
    sdk.Sandbox = SimpleNamespace(from_id=SimpleNamespace(aio=_from_id))
    sdk.exception = SimpleNamespace(NotFoundError=NotFoundError)
    monkeypatch.setitem(sys.modules, "modal", sdk)


@pytest.mark.asyncio
async def test_daytona_says_whether_a_warm_snapshot_still_exists(
    fake_daytona, monkeypatch
):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.snapshot.names.add("oddish-mon-warm-abc")

    assert await starter.warm_image_exists("snapshot:oddish-mon-warm-abc") is True
    assert await starter.warm_image_exists("snapshot:oddish-mon-warm-gone") is False
    assert await starter.warm_image_exists("/oddish/mon.Dockerfile") is None

    async def _down(name):
        raise ConnectionError("daytona down")

    monkeypatch.setattr(fake_daytona.snapshot, "get", _down)
    assert await starter.warm_image_exists("snapshot:oddish-mon-warm-abc") is None


# --- a snapshot boot with the launcher's code -----------------------------------


def _bundle():
    from oddish.workers.launcher.providers.base import CodeBundle

    # The real boot script: its quotes must reach the sandbox's shell intact.
    script = (Path(__file__).resolve().parents[2] / "scripts/mon-boot.sh").read_text()
    return CodeBundle(
        sha256="ab" * 32,
        url="https://bucket.test/b.tar.gz?X-Amz-Signature=secret",
        boot_script=script,
    )


@pytest.mark.asyncio
async def test_modal_boots_a_snapshot_with_the_boot_script_as_its_command(
    fake_modal,
):
    from oddish.mon.contract import BUNDLE_SHA256_VAR, BUNDLE_URL_VAR
    from oddish.workers.launcher.providers.modal import ModalStarter

    bundle = _bundle()
    external_id = await ModalStarter().start(
        name="n", image="im-snap", env={"ODDISH_MON_TOKEN": "j.1.t"}, bundle=bundle
    )
    assert external_id == "sb-1"
    ((args, kwargs),) = fake_modal.created
    assert args == ("sh", "-c", bundle.boot_script)
    assert fake_modal.secrets == [
        {
            "ODDISH_MON_TOKEN": "j.1.t",
            BUNDLE_URL_VAR: bundle.url,
            BUNDLE_SHA256_VAR: bundle.sha256,
        }
    ]
    assert kwargs["secrets"] == ["secret"]
    assert kwargs["image"] == "im-snap"
    assert "X-Amz-Signature" not in repr(args)


@pytest.mark.asyncio
async def test_modal_starts_without_a_bundle_as_before(fake_modal):
    from oddish.workers.launcher.providers.modal import ModalStarter

    await ModalStarter().start(name="n", image="/oddish/mon.Dockerfile", env={"A": "1"})
    ((args, _),) = fake_modal.created
    assert (
        args == (monitor_sandbox.ENTRYPOINT,) == ("/usr/local/bin/mon-entrypoint.sh",)
    )
    assert fake_modal.secrets == [{"A": "1"}]


# --- sandbox_state: has the sandbox itself ended? ----------------------------


@pytest.mark.asyncio
async def test_daytona_sandbox_state_needs_no_monitor_and_reads_an_end_as_gone(
    fake_daytona,
):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.sandbox = _Sandbox()
    execs = list(fake_daytona.sandbox.commands)

    assert not (await starter.sandbox_state("dt-123")).gone
    assert fake_daytona.sandbox.commands == execs  # no exec inside the sandbox
    for state in ("stopped", "error", "destroyed", "build_failed"):
        fake_daytona.sandbox.state = state
        fake_daytona.sandbox.error_reason = "boot refused"
        reply = await starter.sandbox_state("dt-123")
        assert reply.gone and state in reply.detail and "boot refused" in reply.detail
    # A state that is not an end is not read as one.
    fake_daytona.sandbox.state = "unknown"
    assert not (await starter.sandbox_state("dt-123")).gone
    assert (await starter.sandbox_state("dt-missing")).gone


@pytest.mark.asyncio
async def test_daytona_sandbox_state_that_cannot_be_asked_raises(
    fake_daytona, monkeypatch
):
    starter = daytona_starter.DaytonaStarter()

    async def _lookup_failed(external_id):
        raise ConnectionError("daytona down")

    monkeypatch.setattr(fake_daytona, "get", _lookup_failed)
    with pytest.raises(monitor_sandbox.MonitorSandboxError, match="daytona down"):
        await starter.sandbox_state("dt-123")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("poll", "not_found", "gone", "detail"),
    [
        (None, False, False, ""),
        (-1, False, True, "exit code -1"),
        (None, True, True, "not found"),
    ],
)
async def test_modal_sandbox_state_polls_without_an_exec(
    monkeypatch, poll, not_found, gone, detail
):
    from oddish.workers.launcher.providers.modal import ModalStarter

    _fake_modal(monkeypatch, poll=poll, not_found=not_found)

    reply = await ModalStarter().sandbox_state("sb-1")

    assert reply.gone is gone and detail in reply.detail


@pytest.mark.asyncio
async def test_modal_sandbox_state_that_cannot_be_asked_raises(monkeypatch):
    from oddish.workers.launcher.providers.modal import ModalStarter

    _fake_modal(monkeypatch, error=ConnectionError("modal down"))

    with pytest.raises(monitor_sandbox.MonitorSandboxError, match="modal down"):
        await ModalStarter().sandbox_state("sb-1")


# --- the warm-up sandbox's cost span --------------------------------------------


class _WarmUpObserver:
    def __init__(self):
        self.events: list[tuple] = []

    async def started(self, external_id):
        self.events.append(("started", external_id))

    async def ended(self):
        self.events.append(("ended",))
