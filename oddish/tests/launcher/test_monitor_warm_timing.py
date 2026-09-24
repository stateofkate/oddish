"""One ``metric=monitor_warm_timing`` line per warm-up try: the launcher's
phases (create, exec, snapshot) timed where they run, the warm-up's own
(fetch, build) read from the output it already prints."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from test_monitor_image_cache import _SOURCE, store  # noqa: F401 -- a fixture
from test_monitor_sandbox_providers import _Process, fake_daytona  # noqa: F401

from oddish.mon.contract import NOTHING_TO_WARM
from oddish.workers.launcher import image_cache as cache
from oddish.workers.launcher import warm_timing
from oddish.workers.launcher.providers.daytona import DaytonaStarter
from oddish.workers.launcher.providers.modal import ModalStarter

KEYS = [
    "key", "provider", "try", "create_s", "fetch_s", "build_s", "exec_s",
    "snapshot_s", "total_s", "outcome",
]  # fmt: skip
# What ``python -m oddish.mon warm`` prints last, in its log format.
OUTPUT = (
    "2026-09-24 10:00:00,000 oddish-mon INFO oddish.mon.warm: warm-up pulled 1 of 1\n"
    "2026-09-24 10:00:01,000 oddish-mon INFO oddish.mon.warm: "
    "metric=monitor_warm_phases fetch_s=12.50 build_s=80.25\n"
)


class _Aio:
    def __init__(self, fn):
        self.aio = fn


def _slowed(aio, seconds):
    async def _call(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await aio.aio(*args, **kwargs)

    return _Aio(_call)


def _lines(caplog) -> list[dict[str, str]]:
    return [
        dict(field.split("=", 1) for field in r.getMessage().split(" ")[1:])
        for r in caplog.records
        if r.getMessage().startswith("metric=monitor_warm_timing ")
    ]


def _floats(line, *names):
    return [float(line[name]) for name in names]


async def _resolve(starter):
    return await cache.resolve(
        starter,
        monitor_image="/oddish/mon.Dockerfile",
        warm_source=_SOURCE,
        has_registry_login=False,
    )


@pytest.fixture
def slow_modal(fake_modal, monkeypatch):
    import modal

    monkeypatch.setattr(modal.Sandbox, "create", _slowed(modal.Sandbox.create, 0.03))
    sandbox = fake_modal.sandbox
    sandbox.exec = _slowed(sandbox.exec, 0.02)
    sandbox.snapshot_filesystem = _slowed(sandbox.snapshot_filesystem, 0.04)
    fake_modal.output = OUTPUT
    return fake_modal


@pytest.mark.asyncio
async def test_a_snapshot_names_every_phase(store, slow_modal, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)

    resolved = await _resolve(ModalStarter())

    (line,) = _lines(caplog)
    assert list(line) == KEYS
    assert (line["key"], line["provider"], line["try"], line["outcome"]) == (
        resolved.key,
        "modal",
        "1",
        "snapshot",
    )
    # Measured in the sandbox, read from its output.
    assert (line["fetch_s"], line["build_s"]) == ("12.50", "80.25")
    create, exec_, snapshot, total = _floats(
        line, "create_s", "exec_s", "snapshot_s", "total_s"
    )
    assert create >= 0.03 and exec_ >= 0.02 and snapshot >= 0.04
    assert create + exec_ + snapshot <= total < 5


@pytest.mark.asyncio
async def test_nothing_to_warm_takes_no_snapshot(store, slow_modal, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    slow_modal.exit_code = NOTHING_TO_WARM

    await _resolve(ModalStarter())

    (line,) = _lines(caplog)
    assert line["outcome"] == "nothing_to_warm"
    assert float(line["snapshot_s"]) == 0 and float(line["exec_s"]) >= 0.02
    assert line["fetch_s"] == "12.50"


@pytest.mark.asyncio
async def test_each_failed_try_has_its_own_line(store, slow_modal, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    slow_modal.exit_code = 1

    assert (await _resolve(ModalStarter())).outcome == "warm_failed"

    lines = _lines(caplog)
    assert [(x["try"], x["outcome"]) for x in lines] == [
        ("1", "failed"),
        ("2", "failed"),
    ]
    for line in lines:
        # The warm-up printed its phases before it failed.
        assert (line["fetch_s"], line["build_s"]) == ("12.50", "80.25")
        assert float(line["snapshot_s"]) == 0
        assert float(line["create_s"]) >= 0.03 and float(line["exec_s"]) >= 0.02


@pytest.mark.asyncio
async def test_a_cancelled_warm_up_times_the_phase_it_was_in(
    store,  # noqa: F811
    slow_modal,
    caplog,
):
    caplog.set_level(logging.INFO)

    async def _forever():
        await asyncio.Event().wait()

    async def _exec(*args):  # a warm-up that is still building
        return SimpleNamespace(
            wait=_Aio(_forever), stdout=SimpleNamespace(read=_Aio(_forever))
        )

    slow_modal.sandbox.exec = _slowed(_Aio(_exec), 0.02)
    launch = asyncio.create_task(_resolve(ModalStarter()))
    await asyncio.sleep(0.15)
    launch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launch

    (line,) = _lines(caplog)
    assert line["outcome"] == "cancelled"
    assert slow_modal.terminated == 1
    create, exec_, total = _floats(line, "create_s", "exec_s", "total_s")
    assert create >= 0.03 and exec_ >= 0.05 and create + exec_ <= total
    # Nothing came back from the sandbox: its phases are unknown, not guessed.
    assert (line["fetch_s"], line["build_s"]) == ("0.00", "0.00")


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "outcome"), [(0, "snapshot"), (1, "failed")])
async def test_daytona_reads_the_same_line(
    store,  # noqa: F811
    fake_daytona,  # noqa: F811
    monkeypatch,
    caplog,
    code,
    outcome,
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(_Process, "warm_exit_code", code)
    monkeypatch.setattr(_Process, "warm_output", OUTPUT)
    # Warm-up is off on Daytona (ABT-1375); the timing line still holds for it.
    monkeypatch.setattr(DaytonaStarter, "warms_images", True)

    await _resolve(DaytonaStarter())

    lines = _lines(caplog)
    assert lines and all(list(x) == KEYS for x in lines)
    assert {(x["provider"], x["outcome"]) for x in lines} == {("daytona", outcome)}
    assert all((x["fetch_s"], x["build_s"]) == ("12.50", "80.25") for x in lines)


def test_the_last_phases_line_wins_and_a_missing_one_reads_as_zero():
    timing = warm_timing.WarmTiming()
    timing.read_phases(OUTPUT.replace("12.50", "1.00") + OUTPUT + "trailing noise\n")
    assert (timing.fetch_s, timing.build_s) == (12.5, 80.25)
    empty = warm_timing.WarmTiming()
    empty.read_phases("dockerd did not start\n")
    empty.read_phases(None)
    assert (empty.fetch_s, empty.build_s) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_a_provider_called_outside_resolve_prints_no_line(fake_modal, caplog):
    caplog.set_level(logging.INFO)
    fake_modal.output = OUTPUT
    await ModalStarter().build_warm_image(
        image="/oddish/mon.Dockerfile", request=_SOURCE.request
    )
    assert _lines(caplog) == []
