"""Bugbot findings on the monitor sandbox providers and the warm-image cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace

import modal
import pytest

from oddish.config import settings
from oddish.mon.contract import NOTHING_TO_WARM, WarmRequest
from oddish.workers.launcher import image_cache as cache
from oddish.workers.launcher.attempt import WarmSource
from oddish.workers.launcher.providers.base import (
    WARM_IMAGE_READY_SECONDS,
    WARM_UP_SECONDS,
    MonitorSandboxError,
    SandboxProvider,
    SandboxSize,
)
from oddish.workers.launcher.providers.modal import ModalStarter, _modal_size


class _Aio:
    """Stands in for a Modal SDK method: only ``.aio`` is called."""

    def __init__(self, fn):
        self.aio = fn


# --- 1. a GPU-only pin is a pin ---------------------------------------------


@dataclass
class _Res:
    spec_source: str = "pinned"
    cpu_request: float | None = None
    cpu_limit: float | None = None
    mem_request_mb: int | None = None
    mem_limit_mb: int | None = None
    gpu_type: str | None = "H100"
    gpu_count: int = 1


def test_a_gpu_only_pin_still_provisions_the_gpu():
    size = SandboxSize.from_span_resources(_Res())
    assert size is not None
    assert (size.gpu_type, size.gpu_count) == ("H100", 1)
    assert size.cpu == settings.monitor_daytona_cpu
    assert size.memory_mb == settings.monitor_daytona_memory_gb * 1024


# --- 2. a live warm-up is never taken for abandoned -------------------------


def test_warming_ttl_outlasts_a_live_warm_up():
    assert cache.WARMING_TTL_SECONDS >= (
        WARM_UP_SECONDS + settings.monitor_daytona_create_timeout_seconds
    )


# --- 3. a failed warm-up is retried, not remembered as empty ----------------


@pytest.mark.asyncio
async def test_a_failed_warm_up_is_not_cached_as_empty(monkeypatch):
    entries: dict = {}

    async def _read(provider, key):
        return entries.get((provider, key))

    async def _write(provider, key, entry):
        entries[(provider, key)] = entry

    monkeypatch.setattr(cache, "_read", _read)
    monkeypatch.setattr(cache, "_write", _write)

    class _Flaky:
        provider = SandboxProvider.MODAL
        calls = 0

        async def build_warm_image(self, *, image, request, size=None, observer=None):
            self.calls += 1
            if self.calls == 1:
                raise MonitorSandboxError("warm-up exited 1")
            return "im-warm"

    starter = _Flaky()
    source = WarmSource(
        identity="task|v1|", request=WarmRequest(task_archive_url="https://signed")
    )
    args = dict(
        monitor_image="/oddish/mon.Dockerfile",
        warm_source=source,
        has_registry_login=False,
    )

    # Retried at once in the same launch, never remembered as empty.
    assert (await cache.resolve(starter, **args)).image == "im-warm"
    assert [e.state for e in entries.values()] == [cache.CacheState.READY]
    assert starter.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [1, NOTHING_TO_WARM])
async def test_modal_warm_up_tells_failure_from_nothing_to_pull(fake_modal, exit_code):
    fake_modal.exit_code = exit_code
    warm = ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile",
        request=WarmRequest(task_archive_url="https://signed"),
    )
    if exit_code == NOTHING_TO_WARM:
        assert await warm is None
    else:
        with pytest.raises(MonitorSandboxError):
            await warm
    assert fake_modal.terminated == 1


# --- 4. an already-deleted Modal sandbox is gone, as on Daytona -------------


@pytest.fixture
def modal_not_found(monkeypatch):
    async def _from_id(external_id):
        raise modal.exception.NotFoundError(f"no sandbox {external_id}")

    monkeypatch.setattr(modal.Sandbox, "from_id", _Aio(_from_id))


@pytest.mark.asyncio
async def test_modal_stop_of_a_deleted_sandbox_returns_quietly(modal_not_found):
    assert await ModalStarter().stop("sb-gone") is None


@pytest.mark.asyncio
async def test_modal_status_of_a_deleted_sandbox_is_gone(modal_not_found):
    reply = await ModalStarter().status("sb-gone")
    assert reply.exit_code == 1 and not reply.alive


# --- 5. Modal gets the exact GPU type ---------------------------------------


def test_modal_gpu_string_carries_the_exact_type():
    one = SandboxSize(cpu=4, memory_mb=8192, gpu_type="H100", gpu_count=1)
    two = SandboxSize(cpu=4, memory_mb=8192, gpu_type="H100", gpu_count=2)
    a100 = SandboxSize(cpu=4, memory_mb=8192, gpu_type="A100", gpu_count=1)
    assert _modal_size(one)["gpu"] == "H100!"
    assert _modal_size(two)["gpu"] == "H100!:2"
    assert _modal_size(a100)["gpu"] == "A100-40GB"


# --- 6. the warm-up sandbox outlives create, pull and snapshot --------------


# --- 7. the GPU is part of the snapshot identity ----------------------------


@pytest.mark.asyncio
async def test_sizes_differing_only_in_gpu_are_different_snapshots(monkeypatch):
    entries: dict = {}

    async def _read(provider, key):
        return entries.get((provider, key))

    async def _write(provider, key, entry):
        entries[(provider, key)] = entry

    monkeypatch.setattr(cache, "_read", _read)
    monkeypatch.setattr(cache, "_write", _write)

    class _Warm:
        provider = SandboxProvider.DAYTONA

        async def build_warm_image(self, *, image, request, size=None, observer=None):
            return f"snapshot:{size.gpu_type}x{size.gpu_count}"

    source = WarmSource(
        identity="task|v1|", request=WarmRequest(task_archive_url="https://signed")
    )
    sizes = [
        SandboxSize(cpu=4, memory_mb=8192),
        SandboxSize(cpu=4, memory_mb=8192, gpu_type="H100", gpu_count=1),
        SandboxSize(cpu=4, memory_mb=8192, gpu_type="H100", gpu_count=2),
        SandboxSize(cpu=4, memory_mb=8192, gpu_type="H200", gpu_count=1),
    ]
    keys = [
        (
            await cache.resolve(
                _Warm(),
                monitor_image="/oddish/mon.Dockerfile",
                warm_source=source,
                has_registry_login=False,
                size=size,
            )
        )[1]
        for size in sizes
    ]
    assert None not in keys and len(set(keys)) == len(sizes)


# --- 8. a cache hit never names a snapshot Modal has deleted ----------------


@pytest.mark.asyncio
async def test_modal_snapshot_outlives_the_cache_entry_that_names_it(fake_modal):
    image = await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile",
        request=WarmRequest(task_archive_url="https://signed"),
    )
    assert image == "im-warm"
    (snapshot_kwargs,) = fake_modal.snapshots
    assert snapshot_kwargs["timeout"] >= WARM_UP_SECONDS
    assert snapshot_kwargs["ttl"] > WARM_IMAGE_READY_SECONDS
    # Past its ready lifetime an entry is not served, so it cannot outlive the image.
    old = time.time() - WARM_IMAGE_READY_SECONDS
    entry = cache.CacheEntry(state=cache.CacheState.READY, image="im-warm", at=old)
    assert not entry.is_fresh_ready


# --- 9. Modal sandboxes get the same resource floor as Daytona -------------


@pytest.mark.asyncio
async def test_modal_trial_and_warm_up_sandboxes_are_sized(fake_modal):
    fake_modal.exit_code = NOTHING_TO_WARM
    request = WarmRequest(task_archive_url="https://signed")
    given = SandboxSize(cpu=8, memory_mb=16384, headroom_mb=1024)
    await ModalStarter().start(name="t", image="/oddish/mon.Dockerfile", env={})
    await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=request
    )
    await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=request, size=given
    )

    floor = (settings.monitor_daytona_cpu, settings.monitor_daytona_memory_gb * 1024)
    trial, warm_default, warm_given = (kwargs for _, kwargs in fake_modal.created)
    assert (trial["cpu"], trial["memory"]) == floor
    assert (warm_default["cpu"], warm_default["memory"]) == floor
    # The trial's own VM: the task, the headroom and Modal's share of the VM.
    vm = ModalStarter().provisioned(given).memory_mb
    assert vm > 16384 + 1024
    assert (warm_given["cpu"], warm_given["memory"]) == (8, vm)


# --- 10. a Modal provider error in status is no verdict, as on Daytona -----


@pytest.mark.asyncio
async def test_modal_status_provider_error_is_no_verdict(monkeypatch):
    async def _from_id(external_id):
        raise modal.exception.ConnectionError("modal down")

    monkeypatch.setattr(modal.Sandbox, "from_id", _Aio(_from_id))
    with pytest.raises(MonitorSandboxError) as err:
        await ModalStarter().status("sb-1")
    assert isinstance(err.value.__cause__, modal.exception.ConnectionError)

    async def _poll():
        return None

    async def _exec(*args):
        raise modal.exception.InternalError("exec failed")

    sandbox = SimpleNamespace(poll=_Aio(_poll), exec=_Aio(_exec))

    async def _found(external_id):
        return sandbox

    monkeypatch.setattr(modal.Sandbox, "from_id", _Aio(_found))
    with pytest.raises(MonitorSandboxError):
        await ModalStarter().status("sb-1")
