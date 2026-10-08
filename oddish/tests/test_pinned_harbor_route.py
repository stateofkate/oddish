"""Pinned (non-default) Harbor trials run on the worker, and record which Harbor ran."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from oddish.config import settings
from oddish.workers.harbor import _entry
from oddish.workers.jobs import handlers as handlers_module


async def _monitor_drives(monkeypatch, harbor_config: dict) -> bool:
    trial = SimpleNamespace(
        kind="agent", harbor_config=harbor_config, environment="modal"
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
async def test_a_pinned_harbor_stays_on_the_worker(monkeypatch):
    # The monitor runs Harbor's Docker environment in its VM, so a pinned
    # Harbor's Modal code would never execute there.
    pinned = {
        "source": "https://github.com/abundant-ai/harbor",
        "resolved_sha": "c" * 40,
        "variant_id": "ephemeral",
    }
    assert not await _monitor_drives(monkeypatch, pinned)


@pytest.mark.asyncio
@pytest.mark.parametrize("harbor_config", [{}, {"variant_id": "default"}])
async def test_the_default_harbor_keeps_the_monitor(monkeypatch, harbor_config):
    assert await _monitor_drives(monkeypatch, harbor_config)


def test_the_child_records_the_harbor_it_imported(tmp_path):
    (tmp_path / "trial-a").mkdir()

    _entry._write_harbor_runtime(tmp_path, ["trial-a", "trial-missing"])

    record = json.loads((tmp_path / "trial-a" / _entry.HARBOR_RUNTIME_FILE).read_text())
    assert record["version"]
    assert record["module_path"].endswith("harbor")
    assert not (tmp_path / "trial-missing").exists()


def test_a_git_install_records_its_source_and_commit(monkeypatch):
    direct_url = {
        "url": "https://github.com/abundant-ai/harbor",
        "vcs_info": {"vcs": "git", "commit_id": "c" * 40},
    }

    class _Dist:
        version = "0.20.0"

        def read_text(self, name):
            return json.dumps(direct_url) if name == "direct_url.json" else None

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())

    runtime = _entry._harbor_runtime()

    assert runtime["source"] == direct_url["url"]
    assert runtime["commit"] == "c" * 40
    assert runtime["version"] == "0.20.0"


def test_a_failed_record_does_not_fail_the_trial(monkeypatch, tmp_path):
    (tmp_path / "trial-a").mkdir()

    def _boom():
        raise RuntimeError("no metadata")

    monkeypatch.setattr(_entry, "_harbor_runtime", _boom)

    _entry._write_harbor_runtime(tmp_path, ["trial-a"])  # does not raise
