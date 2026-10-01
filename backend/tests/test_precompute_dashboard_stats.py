"""The prepared-read health sample must not block the dashboard precompute."""

from unittest.mock import AsyncMock

import pytest

import dashboard_cache
from oddish.core import experiment_summaries
from worker import functions


@pytest.mark.asyncio
async def test_failed_health_sample_still_runs_precompute(monkeypatch):
    precompute = AsyncMock(return_value=3)
    monkeypatch.setattr(
        dashboard_cache, "precompute_dashboard_queue_pipeline", precompute
    )
    monkeypatch.setattr(
        experiment_summaries,
        "prepared_read_health",
        AsyncMock(
            side_effect=RuntimeError('relation "experiment_summaries" does not exist')
        ),
    )
    monkeypatch.setattr(functions, "close_database_connections", AsyncMock())

    await functions.precompute_dashboard_stats.get_raw_f()()

    precompute.assert_awaited_once()
