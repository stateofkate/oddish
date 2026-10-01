"""The CLI uses the authenticated trial API and reports preparation failures."""

import httpx
import pytest
from typer.testing import CliRunner
from oddish.cli import app
from oddish.cli.detail import _render_trial


@pytest.mark.parametrize("state,code", [("ready", 0), ("absent", 0), ("invalid", 1)])
def test_rebuild_uses_api_and_returns_json(monkeypatch, state, code):
    def post(url, *, headers, timeout):
        assert url == "https://preview.example/trials/robot-0/playback/rebuild"
        assert headers == {"Authorization": "Bearer test"}
        return httpx.Response(
            200, json={"index_status": state}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(
        "oddish.cli.playback.get_auth_headers",
        lambda _: {"Authorization": "Bearer test"},
    )
    monkeypatch.setattr("oddish.cli.playback.httpx.post", post)
    result = CliRunner().invoke(
        app,
        [
            "playback",
            "rebuild",
            "robot-0",
            "--api",
            "https://preview.example",
            "--json",
        ],
    )
    assert result.exit_code == code, result.output
    assert f'"index_status": "{state}"' in result.output


def test_rebuild_surfaces_api_rejection(monkeypatch):
    monkeypatch.setattr("oddish.cli.playback.get_auth_headers", lambda _: {})
    monkeypatch.setattr(
        "oddish.cli.playback.httpx.post",
        lambda url, **_: httpx.Response(
            409,
            json={"detail": "Playback requires a completed trial"},
            request=httpx.Request("POST", url),
        ),
    )
    result = CliRunner().invoke(
        app, ["playback", "rebuild", "robot-0", "--api", "https://preview.example"]
    )
    assert result.exit_code == 1
    assert "409" in result.output
    assert "completed trial" in " ".join(result.output.split())


@pytest.mark.parametrize(
    "playback,text",
    [
        (None, "not prepared"),
        ({"index_status": "absent"}, "absent"),
        ({"index_status": "ready"}, "ready"),
        (
            {"index_status": "invalid", "index_error": "missing_reference"},
            "missing_reference",
        ),
    ],
)
def test_status_reports_playback(capsys, playback, text):
    _render_trial({"id": "robot-0", "playback": playback})
    assert text in capsys.readouterr().out
