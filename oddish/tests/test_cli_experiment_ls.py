"""`oddish experiment ls` sends the dashboard list's params and prints its URL."""

from __future__ import annotations

from typer.testing import CliRunner

from oddish.cli import app
from oddish.cli import experiment as experiment_cli

runner = CliRunner()


class _Response:
    status_code = 200

    def json(self):
        return {
            "experiments": [
                {
                    "id": "exp-1",
                    "name": "nightly",
                    "task_count": 3,
                    "completed_trials": 4,
                    "total_trials": 9,
                    "active_trials": 5,
                    "failed_trials": 1,
                }
            ],
            "experiments_has_more": True,
        }


def test_queue_filter_round_trips_to_api_and_url(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setenv("ODDISH_DASHBOARD_URL", "https://dash.test")
    calls = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            calls.append((url, params))
            return _Response()

    monkeypatch.setattr(experiment_cli.httpx, "Client", _Client)
    result = runner.invoke(
        app,
        [
            "experiment",
            "ls",
            "--api-url",
            "https://api.test",
            "--queue",
            "anthropic/claude-opus-4-5",
            "--queue-state",
            "queued",
            "--status",
            "active",
            "-q",
            "tag:nightly",
            "--page",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "https://api.test/dashboard",
            {
                "include_queues": "false",
                "include_tasks": "false",
                "include_usage": "false",
                "experiments_limit": "25",
                "experiments_offset": "25",
                "experiments_status": "active",
                "experiments_tags": "nightly",
                "experiments_queue": "anthropic/claude-opus-4-5",
                "experiments_queue_state": "queued",
            },
        )
    ]
    assert "exp-1" in result.output and "--page 3" in result.output
    assert (
        "https://dash.test/dashboard?status=active&q=tag%3Anightly"
        "&queue=anthropic%2Fclaude-opus-4-5&queue_state=queued&page=2"
    ) in result.output


def test_queue_state_without_queue_is_rejected(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    result = runner.invoke(app, ["experiment", "ls", "--queue-state", "running"])
    assert result.exit_code == 1
    assert "--queue-state needs --queue" in result.output
