"""One ``metric=monitor_launch`` line per launch, however it ends."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_monitor_launch_handoff import _job, _Starter, _stub_launch, _val

from oddish.workers.launcher import launch as ml
from oddish.workers.launcher.image_cache import Resolved

_real_start = ml._start
_BUNDLE = SimpleNamespace(sha256="b-1")


def _line(capsys) -> dict[str, str]:
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("metric=monitor_launch ")
    ]
    assert len(lines) == 1, lines
    return dict(field.split("=", 1) for field in lines[0].split(" ")[1:])


@pytest.mark.asyncio
async def test_a_handed_off_launch_prints_its_line(monkeypatch, capsys):
    async def _warms(*a, **k):
        await asyncio.sleep(0.05)
        return Resolved("snap-1", "key-1", "warmed")

    _stub_launch(monkeypatch, _Starter(), resolve=_warms)

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    line = _line(capsys)
    # The keys and order before bundle_s, sandbox_create_s and prep_s were
    # added, with the new ones after them: parsers of the old line still work.
    assert list(line) == [
        "job_id", "attempt", "provider", "image", "warm_s", "create_s", "total_s",
        "outcome", "prep_s", "bundle_s", "sandbox_create_s",
    ]  # fmt: skip
    assert (line["job_id"], line["attempt"], line["provider"]) == (
        "job-1",
        "1",
        "modal",
    )
    assert (line["image"], line["outcome"]) == ("warmed", "handed_off")
    assert float(line["warm_s"]) >= 0.05 > float(line["create_s"])
    assert float(line["total_s"]) >= float(line["warm_s"])
    assert float(line["bundle_s"]) == 0  # a fresh snapshot of a faked start


@pytest.mark.asyncio
async def test_a_cache_hit_names_the_bundle_the_start_and_what_came_before(
    monkeypatch, capsys, tmp_path
):
    """3 of 10 staging hits spent 16-23 s outside warm_s and create_s."""

    async def _hit(*a, **k):
        return Resolved("snap-1", "key-1", "hit")

    class _SlowStarter(_Starter):
        async def start(self, *, name, image, env, size=None, bundle=None):
            await asyncio.sleep(0.05)
            self.started.append((image, bundle))
            return "ext-1"

    async def _bundle(root):
        await asyncio.sleep(0.03)
        return _BUNDLE

    async def _slow_facts(*a, **k):
        await asyncio.sleep(0.04)
        return None, None, {}

    starter = _SlowStarter()
    _stub_launch(monkeypatch, starter, resolve=_hit)
    monkeypatch.setattr(ml, "_start", _real_start)
    monkeypatch.setattr(ml, "_task_facts", _slow_facts)
    monkeypatch.setattr(ml.code_bundle, "prepare", _bundle)
    dockerfile = tmp_path / "mon.Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    monkeypatch.setattr(ml.settings, "monitor_image", str(dockerfile))

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    line = _line(capsys)
    assert (line["image"], line["outcome"]) == ("hit", "handed_off")
    assert starter.started == [("snap-1", _BUNDLE)]
    prep, bundle, sandbox, create, total = (
        float(line[k])
        for k in ("prep_s", "bundle_s", "sandbox_create_s", "create_s", "total_s")
    )
    assert prep >= 0.04 and bundle >= 0.03 and sandbox >= 0.05
    assert bundle + sandbox <= create
    assert prep + float(line["warm_s"]) + create <= total


@pytest.mark.asyncio
async def test_a_cold_start_has_no_bundle(monkeypatch, capsys):
    class _SlowStarter(_Starter):
        async def start(self, *, name, image, env, size=None, bundle=None):
            await asyncio.sleep(0.05)
            self.started.append((image, bundle))
            return "ext-1"

    starter = _SlowStarter()
    _stub_launch(monkeypatch, starter)
    monkeypatch.setattr(ml, "_start", _real_start)

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    line = _line(capsys)
    assert line["image"] == "cold" and starter.started == [("img", None)]
    assert float(line["bundle_s"]) == 0
    assert 0.05 <= float(line["sandbox_create_s"]) <= float(line["create_s"])


@pytest.mark.asyncio
async def test_a_re_warmed_snapshot_is_image_gone_and_its_warm_up_is_warm_s(
    monkeypatch, capsys
):
    lookups = [
        Resolved("snap-1", "key-1", "hit"),
        Resolved("snap-2", "key-1", "warmed"),
    ]

    async def _resolve(*a, **k):
        if len(lookups) == 1:
            await asyncio.sleep(0.05)  # the re-warm
        return lookups.pop(0)

    starter = _Starter()
    _stub_launch(monkeypatch, starter, resolve=_resolve)

    async def _start(_starter, job, image, env, size):
        starter.started.append(image)
        if image == "snap-1":
            raise ml.monitor_sandbox.WarmImageUnusable("booted 2/4, needs 4/16")
        return "ext-1"

    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml.monitor_image_cache, "forget", lambda *a: _val(None))

    assert (await ml.launch_monitor_attempt(_job())).handoff is not None

    line = _line(capsys)
    assert (line["image"], line["outcome"]) == ("gone", "handed_off")
    assert float(line["warm_s"]) >= 0.05 > float(line["create_s"])
    assert starter.started == ["snap-1", "snap-2"]
    # Both starts count; the re-warm between them is warm_s.
    assert float(line["sandbox_create_s"]) <= float(line["create_s"])


@pytest.mark.asyncio
async def test_a_failed_launch_prints_its_error_type(monkeypatch, capsys):
    """A launch that fails before create settles the attempt, with its reason."""

    async def _lookup_fails(*a, **k):
        raise RuntimeError("no snapshot after waiting 1800 s")

    starter = _Starter()
    calls = _stub_launch(monkeypatch, starter, resolve=_lookup_fails)

    assert await ml.launch_monitor_attempt(_job()) is None

    line = _line(capsys)
    assert (line["image"], line["outcome"]) == ("none", "failed")
    assert line["error_type"] == "RuntimeError"
    assert (line["bundle_s"], line["sandbox_create_s"]) == ("0.00", "0.00")
    assert starter.started == []  # never a cold boot
    (settled,) = calls.settled
    assert settled["execution"].retryable is True
    assert "no snapshot after waiting 1800 s" in settled["execution"].execution_error


@pytest.mark.asyncio
async def test_a_launch_with_nothing_to_run_prints_skipped(monkeypatch, capsys):
    _stub_launch(monkeypatch, _Starter())
    monkeypatch.setattr(ml, "prepare_trial_run", lambda **k: _val(None))

    assert await ml.launch_monitor_attempt(_job()) is None

    line = _line(capsys)
    assert (line["provider"], line["image"], line["outcome"]) == (
        "none",
        "none",
        "skipped",
    )
    assert "error_type" not in line


@pytest.mark.asyncio
async def test_a_launch_that_raises_prints_failed(monkeypatch, capsys):
    _stub_launch(monkeypatch, _Starter())
    monkeypatch.setattr(ml.monitor_sandbox, "provider_for_environment", lambda e: None)

    with pytest.raises(RuntimeError, match="No monitor sandbox starter"):
        await ml.launch_monitor_attempt(_job())

    line = _line(capsys)
    assert (line["outcome"], line["error_type"]) == ("failed", "RuntimeError")
