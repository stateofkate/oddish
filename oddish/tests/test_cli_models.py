"""CLI coverage for ``oddish models``."""

from __future__ import annotations

import importlib
import json

import pytest
from typer.testing import CliRunner

from oddish.cli import app

models_cli = importlib.import_module("oddish.cli.models")

runner = CliRunner()

PAYLOAD = {
    "models": [
        {
            "model": "anthropic/claude-sonnet-5-5",
            "provider": "anthropic",
            "limit": 80,
            "running": 12,
            "queued": 4,
            "health": "healthy",
            "last_checked_at": "2026-10-13T00:00:00Z",
        },
        {
            "model": "zai/glm-5.3",
            "provider": "zai",
            "limit": 300,
            "running": 0,
            "queued": 0,
            "health": "failing",
            "last_checked_at": "2026-10-13T00:00:00Z",
        },
    ],
    "timestamp": "2026-10-13T00:00:00",
}


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload

    @property
    def text(self):
        return json.dumps(self.payload)


def _install_client(monkeypatch, response: _Response) -> list[str]:
    urls: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            urls.append(url)
            return response

    monkeypatch.setattr(models_cli.httpx, "Client", _Client)
    return urls


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")


def test_models_renders_limits_load_and_health(monkeypatch):
    urls = _install_client(monkeypatch, _Response(200, PAYLOAD))

    result = runner.invoke(app, ["models", "--api", "http://api.test/"])

    assert result.exit_code == 0, result.output
    assert urls == ["http://api.test/models"]
    assert "anthropic/claude-sonnet-5-5" in result.output
    assert "healthy" in result.output
    assert "zai/glm-5.3" in result.output
    assert "failing" in result.output


def test_models_json_is_the_raw_response(monkeypatch):
    _install_client(monkeypatch, _Response(200, PAYLOAD))

    result = runner.invoke(app, ["models", "--json", "--api", "http://api.test"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == PAYLOAD


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (403, "only to Abundant organization keys"),
        (404, "needs a newer Oddish server"),
        (500, "500 - boom"),
    ],
)
def test_models_explains_failures(monkeypatch, status_code, message):
    _install_client(monkeypatch, _Response(status_code, {"detail": "boom"}))

    result = runner.invoke(app, ["models", "--api", "http://api.test"])

    assert result.exit_code == 1
    assert message in result.output
