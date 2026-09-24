"""ODDISH_LOCAL_MODE runs the shared queue worker inside the API lifespan."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_local_api_lifespan_runs_canonical_queue_worker(monkeypatch, tmp_path):
    from api import app as app_module
    from worker import byok_resolver

    installed = []
    worker_started = asyncio.Event()
    worker_cancelled = []

    async def no_startup_work():
        return None

    async def run_worker():
        worker_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            worker_cancelled.append(True)
            raise

    monkeypatch.setattr(app_module.settings, "local_mode", True)
    monkeypatch.setattr(app_module.settings, "harbor_jobs_dir", str(tmp_path))
    monkeypatch.setattr(
        app_module, "_assert_quota_schema_or_force_off", no_startup_work
    )
    monkeypatch.setattr(app_module, "_apply_role_defaults_bg", no_startup_work)
    monkeypatch.setattr(app_module, "close_database_connections", no_startup_work)
    monkeypatch.setattr("dashboard_cache.install_modal_dashboard_cache", lambda: None)
    monkeypatch.setattr(
        byok_resolver, "install_byok_resolver", lambda: installed.append(True)
    )
    monkeypatch.setattr(
        "oddish.workers.queue.queue_manager.run_polling_worker", run_worker
    )

    async with app_module.lifespan(None):
        assert installed == [True]
        await asyncio.wait_for(worker_started.wait(), timeout=1)

    assert worker_cancelled == [True]
