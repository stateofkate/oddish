"""ShareCostsMiddleware zeroes cost figures only when the share hides them."""

import json

import httpx
import pytest
from api.share_costs import ShareCostsMiddleware
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from oddish.core.sharing.helpers import share_hides_costs

PAYLOAD = {
    "cost_usd": 1.5,
    "show_costs": True,
    "trials": [{"qa_cost_usd": 2}],
    "effort": {"steps_sum": 20, "cost": {"trial_sum": 10, "qa_sum": 2}},
}


def _app(hide: bool) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ShareCostsMiddleware)

    @app.get("/public/json")
    async def as_json():
        share_hides_costs.set(hide)
        return PAYLOAD

    @app.get("/public/ndjson")
    async def as_ndjson():
        share_hides_costs.set(hide)

        async def lines():
            yield json.dumps(PAYLOAD)[:10]
            yield json.dumps(PAYLOAD)[10:] + "\n" + json.dumps(PAYLOAD) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.get("/public/trials/t/files/result.json")
    async def as_file():
        share_hides_costs.set(hide)
        return PAYLOAD

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("hide", [True, False])
async def test_costs_zeroed_only_when_hidden(hide):
    expected = (
        {
            "cost_usd": 0,
            "show_costs": True,
            "trials": [{"qa_cost_usd": 0}],
            "effort": {"steps_sum": 20, "cost": None},
        }
        if hide
        else PAYLOAD
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(hide)), base_url="http://test"
    ) as client:
        assert (await client.get("/public/json")).json() == expected
        body = (await client.get("/public/ndjson")).text
    assert [json.loads(line) for line in body.splitlines()] == [expected, expected]


@pytest.mark.asyncio
async def test_file_downloads_pass_through_untouched():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(True)), base_url="http://test"
    ) as client:
        response = await client.get("/public/trials/t/files/result.json")
    assert response.json() == PAYLOAD
