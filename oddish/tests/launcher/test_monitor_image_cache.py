"""The warm-image cache: what to pull, how it is keyed, and that it can only help."""

from __future__ import annotations

import pytest

from oddish.mon import warm
from oddish.mon.contract import WarmRequest
from oddish.workers.launcher import image_cache as cache
from oddish.workers.launcher.attempt import WarmSource
from oddish.workers.launcher.providers.base import SandboxProvider


# --- what a warm-up pulls -------------------------------------------------


def _task(tmp_path, dockerfile=None, compose=None, toml=None):
    env = tmp_path / "environment"
    env.mkdir(parents=True)
    if dockerfile is not None:
        (env / "Dockerfile").write_text(dockerfile)
    if compose is not None:
        (env / "docker-compose.yaml").write_text(compose)
    (tmp_path / "task.toml").write_text(toml or "")
    return tmp_path


def test_base_images_come_from_the_dockerfile(tmp_path):
    task = _task(
        tmp_path,
        dockerfile=(
            "FROM --platform=linux/amd64 golang:1.22 AS build\nRUN go build\n"
            "FROM public.ecr.aws/x/mars-base:latest\nCOPY --from=build /a /a\n"
            "FROM build AS again\nFROM scratch\nFROM ${BASE}\n"
        ),
    )
    # Stage aliases, scratch and unresolved build args are not pullable.
    assert warm.image_refs(task) == ["golang:1.22", "public.ecr.aws/x/mars-base:latest"]


def test_compose_images_are_included_once(tmp_path):
    task = _task(
        tmp_path,
        dockerfile="FROM ubuntu:24.04\n",
        compose="services:\n  main: {}\n  db:\n    image: redis:7\n  db2:\n    image: 'redis:7'\n",
    )
    assert warm.image_refs(task) == ["ubuntu:24.04", "redis:7"]


def test_a_prebuilt_image_replaces_the_build(tmp_path):
    task = _task(tmp_path, dockerfile="FROM ubuntu:24.04\n")
    assert warm.image_refs(task, "registry/built:abc") == ["registry/built:abc"]


def test_a_declared_docker_image_wins_over_the_dockerfile(tmp_path):
    task = _task(
        tmp_path,
        dockerfile="FROM ubuntu:24.04\n",
        toml='[environment]\ndocker_image = "ubuntu:22.04"\n',
    )
    assert warm.image_refs(task) == ["ubuntu:22.04"]


def test_a_task_with_nothing_to_pull(tmp_path):
    assert warm.image_refs(_task(tmp_path)) == []


# --- the key ---------------------------------------------------------------


def test_key_changes_with_provider_task_and_monitor_code(tmp_path):
    cache.monitor_identity.cache_clear()
    dockerfile = tmp_path / "mon.Dockerfile"
    dockerfile.write_text("FROM python:3.13\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")

    base = cache.cache_key("modal", str(dockerfile), "task|v1|")
    assert base == cache.cache_key("modal", str(dockerfile), "task|v1|")
    assert base != cache.cache_key("daytona", str(dockerfile), "task|v1|")
    assert base != cache.cache_key("modal", str(dockerfile), "task|v2|")

    # New monitor code must never boot a snapshot taken from the old code.
    (tmp_path / "src" / "a.py").write_text("x = 2\n")
    cache.monitor_identity.cache_clear()
    assert base != cache.cache_key("modal", str(dockerfile), "task|v1|")


# --- resolve: hit, miss, herd, failure --------------------------------------


class _Starter:
    provider = SandboxProvider.MODAL

    def __init__(self, image="im-warm", error=None):
        self.image, self.error, self.calls = image, error, []

    async def build_warm_image(self, *, image, request, size=None):
        self.calls.append((image, request))
        self.sizes = [*getattr(self, "sizes", []), size]
        if self.error:
            raise self.error
        return self.image


@pytest.fixture
def store(monkeypatch):
    entries: dict[tuple[str, str], dict] = {}

    async def _read(provider, key):
        return entries.get((provider, key))

    async def _write(provider, key, entry):
        entries[(provider, key)] = entry

    monkeypatch.setattr(cache, "_read", _read)
    monkeypatch.setattr(cache, "_write", _write)
    return entries


_SOURCE = WarmSource(
    identity="task|v1|", request=WarmRequest(task_archive_url="https://signed")
)


async def _resolve(starter, **overrides):
    args = dict(monitor_image="/oddish/mon.Dockerfile", warm_source=_SOURCE, has_registry_login=False)
    return await cache.resolve(starter, **{**args, **overrides})


@pytest.mark.asyncio
async def test_first_launch_warms_and_the_next_one_hits(store):
    starter = _Starter()

    assert await _resolve(starter) == ("im-warm", next(iter(store))[1])
    assert await _resolve(starter) == ("im-warm", next(iter(store))[1])

    assert len(starter.calls) == 1  # warmed once
    # The warm-up gets where the task is, never the cache identity or a secret.
    assert starter.calls[0][1] == _SOURCE.request
    assert set(WarmRequest.model_fields) == {"task_archive_url", "task_files", "docker_image"}


@pytest.mark.asyncio
async def test_a_private_registry_login_is_never_cached(store):
    starter = _Starter()
    assert await _resolve(starter, has_registry_login=True) == ("/oddish/mon.Dockerfile", None)
    assert starter.calls == [] and store == {}


@pytest.mark.asyncio
async def test_concurrent_launches_do_not_all_warm(store):
    starter = _Starter()
    key = cache.cache_key("modal", "/oddish/mon.Dockerfile", _SOURCE.identity)
    store[("modal", key)] = cache.CacheEntry(state=cache.CacheState.WARMING)

    assert await _resolve(starter) == ("/oddish/mon.Dockerfile", None)  # cold, this once
    assert starter.calls == []


@pytest.mark.asyncio
async def test_an_abandoned_warm_up_is_retried(store):
    starter = _Starter()
    key = cache.cache_key("modal", "/oddish/mon.Dockerfile", _SOURCE.identity)
    store[("modal", key)] = cache.CacheEntry(
        state=cache.CacheState.WARMING,
        at=cache.time.time() - cache.WARMING_TTL_SECONDS - 1,
    )

    assert (await _resolve(starter))[0] == "im-warm"


@pytest.mark.asyncio
async def test_a_task_with_nothing_to_pull_is_remembered(store):
    starter = _Starter(image=None)
    assert await _resolve(starter) == ("/oddish/mon.Dockerfile", None)
    assert await _resolve(starter) == ("/oddish/mon.Dockerfile", None)
    assert len(starter.calls) == 1


@pytest.mark.asyncio
async def test_a_failing_warm_up_never_fails_the_launch(store):
    starter = _Starter(error=RuntimeError("provider down"))
    assert await _resolve(starter) == ("/oddish/mon.Dockerfile", None)


@pytest.mark.asyncio
async def test_a_provider_without_snapshots_runs_cold(store):
    class _Plain:
        provider = SandboxProvider.DAYTONA  # a provider with no build_warm_image

    assert await _resolve(_Plain()) == ("/oddish/mon.Dockerfile", None)


@pytest.mark.asyncio
async def test_a_refused_snapshot_is_forgotten(store):
    starter = _Starter()
    _, key = await _resolve(starter)
    await cache.forget("modal", key)
    assert store[("modal", key)].state is cache.CacheState.INVALID
    assert (await _resolve(starter))[0] == "im-warm"  # warmed again
    assert len(starter.calls) == 2


def test_a_cache_entry_survives_storage_and_rejects_an_unknown_state():
    entry = cache.CacheEntry(state=cache.CacheState.READY, image="im-1")
    assert cache.CacheEntry.model_validate_json(entry.model_dump_json()) == entry
    with pytest.raises(ValueError):
        cache.CacheEntry.model_validate({"state": "half-baked"})


@pytest.mark.asyncio
async def test_a_different_size_is_a_different_snapshot(store):
    from oddish.workers.launcher.providers.base import SandboxSize

    starter = _Starter()
    small, big = SandboxSize(cpu=1, memory_mb=2048), SandboxSize(cpu=4, memory_mb=16384)
    _, key_small = await _resolve(starter, size=small)
    _, key_big = await _resolve(starter, size=big)
    await _resolve(starter, size=big)

    assert key_small != key_big
    assert starter.sizes == [small, big]  # the warm-up runs at the task's size
