"""New CLI browse modes reach the shared core on standalone servers too."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from oddish.server import api


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ids_only", "count_only"])
async def test_browse_selection_forwards_filters(mode):
    @asynccontextmanager
    async def session():
        yield object()

    browse = AsyncMock(
        return_value=[f"t{i}" for i in range(5001)] if mode == "ids_only" else 234
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        with (
            patch("oddish.server.get_read_session", session),
            patch("oddish.server.browse_tasks_core", browse),
        ):
            response = await client.get(
                "/tasks/browse",
                params={
                    mode: "true",
                    "delivered_to": "xai,TML",
                    "not_delivered_to": "GDM",
                    "never_delivered": "false",
                    "categories": "swe",
                    "qa_outcomes": "accepted",
                    "exclude_delivery_id": "d1",
                    "steps_p50_min": 0,
                    "steps_p50_max": 200,
                    "agent_count_min": 3,
                    "sort": "total_trials_desc",
                    "trial_finished_after": "2026-09-01T00:00:00",
                },
            )
    assert response.status_code == 200, response.text
    kwargs = browse.await_args.kwargs
    assert kwargs[mode] is True
    assert kwargs["delivered_to"] == ["xai", "TML"]
    assert kwargs["not_delivered_to"] == ["GDM"]
    assert kwargs["never_delivered"] is False
    assert kwargs["categories"] == ["swe"]
    assert kwargs["qa_outcomes"] == ["accepted"]
    assert kwargs["exclude_delivery_id"] == "d1"
    assert kwargs["steps_p50_min"] == 0
    assert kwargs["steps_p50_max"] == 200
    assert kwargs["agent_count_min"] == 3
    assert kwargs["sort"] == "total_trials_desc"
    assert kwargs["trial_finished_after"].isoformat() == "2026-09-01T00:00:00"
    if mode == "ids_only":
        assert len(response.json()["ids"]) == 5000
        assert response.json()["truncated"] is True
    else:
        assert response.json() == {"total": 234}


@pytest.mark.asyncio
async def test_filter_options_route():
    @asynccontextmanager
    async def session():
        yield object()

    facets = AsyncMock(return_value={"delivery_customers": ["xai", "unmapped"]})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        with (
            patch("oddish.server.get_read_session", session),
            patch("oddish.server.browse_task_facets_core", facets),
        ):
            response = await client.get("/tasks/browse/facets")
    assert response.status_code == 200
    assert response.json()["delivery_customers"] == ["xai", "unmapped"]
    facets.assert_awaited_once()
