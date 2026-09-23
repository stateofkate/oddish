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
        assert all(hasattr(starter, method) for method in ("start", "stop", "status"))


def test_an_unknown_provider_is_refused():
    with pytest.raises(monitor_sandbox.MonitorSandboxError):
        monitor_sandbox.get_starter("nope")


@pytest.mark.asyncio
@pytest.mark.parametrize(("environment", "drives"), [("modal", True), ("daytona", True), ("gke", False)])
async def test_the_gate_keeps_unsupported_environments_on_the_worker(monkeypatch, environment, drives):
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

    async def exec(self, command, timeout=None):
        self._sandbox.commands.append(command)
        code = self.warm_exit_code if command.endswith(" warm") else 0
        return SimpleNamespace(exit_code=code, result='{"alive": true}')


class _Sandbox:
    def __init__(self, params=None, state="started"):
        self.id, self.state, self.params, self.commands = "dt-123", state, params, []
        self.process = _Process(self)
        self.snapshots: list[str] = []
        self.cpu, self.memory = 0, 0

    async def _experimental_create_snapshot(self, name, timeout=None):
        self.snapshots.append(name)


class _FakeDaytona:
    def __init__(self):
        self.created, self.deleted, self.sandbox = [], [], None

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
    monkeypatch.setattr(daytona_starter, "_image", lambda image: f"image:{image}")
    monkeypatch.setattr(daytona_starter, "_not_found_errors", lambda: (LookupError,))
    sdk = types.ModuleType("daytona")
    sdk.CreateSandboxFromImageParams = lambda **kwargs: SimpleNamespace(**kwargs)
    sdk.CreateSandboxFromSnapshotParams = lambda **kwargs: SimpleNamespace(**kwargs)
    sdk.Resources = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "daytona", sdk)
    return fake


@pytest.mark.asyncio
async def test_start_passes_secrets_at_creation_and_starts_the_monitor_once(fake_daytona):
    env = {"ODDISH_MON_TOKEN": "j.1.t", "ODDISH_MON_API_URL": "https://api"}
    external_id = await daytona_starter.DaytonaStarter().start(
        name="oddish-mon-wj-1", image="/oddish/mon.Dockerfile", env=env
    )

    params, _timeout = fake_daytona.created[0]
    assert external_id == "dt-123"
    assert params.env_vars == env  # the provider's creation slot, nowhere else
    assert params.image == "image:/oddish/mon.Dockerfile"
    assert params.auto_stop_interval == settings.monitor_attempt_deadline_seconds // 60
    # Exactly one start, by the starter. Two monitors run the trial twice.
    (command,) = fake_daytona.sandbox.commands
    assert daytona_starter.ENTRYPOINT in command


def test_the_image_never_starts_the_monitor_itself():
    """Providers differ in whether they run an image's ENTRYPOINT, and a
    snapshot boot may differ again. The starter is the only place it starts."""
    dockerfile = (Path(__file__).resolve().parents[2] / "mon.Dockerfile").read_text()
    instructions = [line.split()[0] for line in dockerfile.splitlines() if line.strip()]
    assert "ENTRYPOINT" not in instructions and "CMD" not in instructions


@pytest.mark.asyncio
async def test_a_monitor_that_cannot_be_started_does_not_leave_a_sandbox(
    fake_daytona, monkeypatch
):
    async def _boom(self, command, timeout=None):
        raise RuntimeError("exec refused")

    monkeypatch.setattr(_Process, "exec", _boom)
    with pytest.raises(RuntimeError):
        await daytona_starter.DaytonaStarter().start(name="n", image="img", env={})
    assert fake_daytona.deleted == ["dt-123"]


@pytest.mark.asyncio
async def test_status_asks_the_monitor_and_reports_a_gone_sandbox(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    await starter.start(name="n", image="img", env={})

    reply = await starter.status("dt-123")
    assert reply.alive and reply.output == '{"alive": true}'
    fake_daytona.sandbox.state = "stopped"
    assert await starter.status("dt-123") == monitor_sandbox.StatusReply.gone()
    assert not (await starter.status("dt-missing")).alive


@pytest.mark.asyncio
async def test_stop_deletes_one_sandbox_by_id_and_tolerates_a_missing_one(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    await starter.start(name="n", image="img", env={})

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


@pytest.mark.asyncio
async def test_daytona_gets_the_tasks_size_rounded_up_to_whole_units(fake_daytona):
    await daytona_starter.DaytonaStarter().start(
        name="n", image="/oddish/mon.Dockerfile", env={},
        size=SandboxSize(cpu=3.5, memory_mb=15000),
    )  # fmt: skip
    resources = fake_daytona.created[0][0].resources
    assert (resources.cpu, resources.memory) == (4, 15)
    assert resources.disk == settings.monitor_daytona_disk_gb


@pytest.mark.asyncio
async def test_daytona_without_a_size_uses_the_configured_default(fake_daytona):
    await daytona_starter.DaytonaStarter().start(
        name="n", image="/oddish/mon.Dockerfile", env={}
    )
    resources = fake_daytona.created[0][0].resources
    assert resources.cpu == settings.monitor_daytona_cpu
    assert resources.memory == settings.monitor_daytona_memory_gb


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

    monkeypatch.setattr(monitor_launch, "_begin_judge_costs_if_enabled", _no_judge_costs)
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


@pytest.mark.asyncio
async def test_daytona_warms_at_the_tasks_size_without_secrets(fake_daytona):
    from oddish.mon.contract import WarmRequest
    from oddish.mon.warm import WARM_VAR

    image = await daytona_starter.DaytonaStarter().build_warm_image(
        image="/oddish/mon.Dockerfile",
        request=WarmRequest(task_archive_url="https://signed"),
        size=SandboxSize(cpu=4, memory_mb=16384),
    )
    params, _ = fake_daytona.created[0]
    name = image.removeprefix(daytona_starter.SNAPSHOT_PREFIX)
    assert name.startswith(daytona_starter.SNAPSHOT_NAME_PREFIX)
    assert list(params.env_vars) == [WARM_VAR]  # no token, no model key
    # The warm command runs once; the monitor itself is never started here.
    assert fake_daytona.sandbox.commands[0] == f"{daytona_starter.ENTRYPOINT} warm"
    assert not any("nohup" in c for c in fake_daytona.sandbox.commands)
    assert (params.resources.cpu, params.resources.memory) == (4, 16)
    assert fake_daytona.sandbox.snapshots == [name]
    assert fake_daytona.deleted == ["dt-123"]  # the warm-up sandbox only


@pytest.mark.asyncio
async def test_nothing_pulled_means_no_snapshot(fake_daytona, monkeypatch):
    from oddish.mon.contract import WarmRequest

    from oddish.mon.warm import NOTHING_TO_WARM

    # The task names no image: nothing to snapshot, and not a failure.
    monkeypatch.setattr(_Process, "warm_exit_code", NOTHING_TO_WARM)
    image = await daytona_starter.DaytonaStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=WarmRequest()
    )
    assert image is None and fake_daytona.sandbox.snapshots == []

    # A pull that failed is an error the cache must not remember as "empty".
    monkeypatch.setattr(_Process, "warm_exit_code", 1)
    with pytest.raises(monitor_sandbox.MonitorSandboxError):
        await daytona_starter.DaytonaStarter().build_warm_image(
            image="/oddish/mon.Dockerfile", request=WarmRequest()
        )


@pytest.mark.asyncio
async def test_a_snapshot_boots_without_resources_and_its_size_is_checked(fake_daytona):
    starter = daytona_starter.DaytonaStarter()
    fake_daytona.boot_size = (4, 16)
    await starter.start(
        name="n", image="snapshot:oddish-mon-warm-abc", env={},
        size=SandboxSize(cpu=4, memory_mb=16384),
    )  # fmt: skip
    params, _ = fake_daytona.created[0]
    assert params.snapshot == "oddish-mon-warm-abc"
    assert not hasattr(params, "resources")

    # The wrong size is refused and the sandbox removed: the launcher then
    # forgets the cache entry and starts cold at the right size.
    fake_daytona.boot_size = (2, 4)
    with pytest.raises(monitor_sandbox.MonitorSandboxError):
        await starter.start(
            name="n", image="snapshot:oddish-mon-warm-abc", env={},
            size=SandboxSize(cpu=4, memory_mb=16384),
        )  # fmt: skip
    assert fake_daytona.deleted == ["dt-123"]


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


@pytest.mark.asyncio
async def test_a_snapshot_boot_starts_the_monitor_the_same_way(fake_daytona):
    fake_daytona.boot_size = (1, 3)
    await daytona_starter.DaytonaStarter().start(
        name="n", image="snapshot:oddish-mon-warm-abc", env={},
        size=SandboxSize(cpu=1, memory_mb=3072),
    )  # fmt: skip
    (command,) = fake_daytona.sandbox.commands
    assert command.startswith(f"nohup {daytona_starter.ENTRYPOINT}")
    assert fake_daytona.deleted == []




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


