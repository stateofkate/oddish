"""--modal-gvisor: Modal's default gVisor sandbox instead of a Modal VM."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import typer
from oddish.config import settings
from oddish.workers.jobs import handlers as handlers_module

run_module = importlib.import_module("oddish.cli.run")


def test_the_option_turns_the_vm_runtime_off():
    assert run_module._modal_gvisor_environment_kwargs(["region=us-east"]) == [
        "region=us-east",
        "modal_vm_runtime=false",
    ]
    assert run_module._modal_gvisor_environment_kwargs(None) == [
        "modal_vm_runtime=false"
    ]


def test_the_option_refuses_a_conflicting_runtime_kwarg():
    with pytest.raises(typer.BadParameter, match="modal_vm_runtime"):
        run_module._modal_gvisor_environment_kwargs(["modal_vm_runtime=true"])


async def _drives(monkeypatch, *, environment: str, kwargs: dict | None) -> bool:
    harbor_config = {} if kwargs is None else {"environment": {"kwargs": kwargs}}
    trial = SimpleNamespace(
        kind="agent", harbor_config=harbor_config, environment=environment
    )

    class _Session:
        async def get(self, _model, _id):
            return trial

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    monkeypatch.setattr(handlers_module, "get_session", _get_session)
    monkeypatch.setattr(settings, "monitor_sandbox_starter", None)
    monkeypatch.setattr(settings, "monitor_api_url", "https://api.test")
    return await handlers_module._monitor_can_drive("trial-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, 0, "false", "False", "0"])
async def test_a_gvisor_trial_stays_on_the_worker(monkeypatch, value):
    # The monitor sandbox is always a Modal VM; only the worker's Harbor
    # Modal environment can start a gVisor sandbox.
    assert not await _drives(
        monkeypatch, environment="modal", kwargs={"modal_vm_runtime": value}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        None,
        {},
        {"modal_vm_runtime": True},
        {"modal_vm_runtime": 1},
        {"region": "us-east"},
    ],
)
async def test_other_modal_trials_keep_the_monitor(monkeypatch, kwargs):
    assert await _drives(monkeypatch, environment="modal", kwargs=kwargs)
