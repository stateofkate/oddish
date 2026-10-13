from __future__ import annotations

import importlib
from datetime import datetime, timezone

import httpx
from typer.testing import CliRunner

from oddish.cli import app

# The package re-exports the command function under the module's name.
cli = importlib.import_module("oddish.cli.adversarial")


def _trial(n, *, agent="mini-swe-agent", status="success", reward=0.0, pins=None):
    return {
        "id": f"t-{n}",
        "agent": agent,
        "status": status,
        "reward": reward,
        "attempts": 1,
        "harbor_config": {"adversarial": pins or {}},
    }


TRIALS = [
    _trial(1, reward=1.0, pins={"white_box": True}),
    _trial(2),
    _trial(3, reward=1.0),
    _trial(9, agent=cli.REPLAY_AGENT, reward=1.0, pins={"replay_of": "t-1", "attack_attempt": 1}),
    _trial(10, agent=cli.REPLAY_AGENT, reward=0.0, pins={"replay_of": "t-3", "attack_attempt": 1}),
    _trial(4),
    _trial(11, agent=cli.REPLAY_AGENT, reward=0.0, pins={"replay_of": "t-4", "attack_attempt": 1}),
]
# GET /tasks/{id} is the status view: no versions or findings.
TASK = {"current_version_id": "t-v5", "current_version": 5}
PANEL = {
    "version": {
        "version": 5,
        "retained_findings": [{"source": "adversarial", "replay_trial_id": "t-11"}],
    }
}


class _Client:
    posted: dict = {}
    trials_status = 200

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        if url.endswith("/tasks/t"):
            return httpx.Response(200, json=TASK, request=httpx.Request("GET", url))
        if url.endswith("/tasks/t/panel"):
            assert params == {"version": 5}
            return httpx.Response(200, json=PANEL, request=httpx.Request("GET", url))
        if type(self).trials_status != 200:
            return httpx.Response(
                type(self).trials_status, json={}, request=httpx.Request("GET", url)
            )
        assert params == {"include_adversarial": "true", "version": 5}
        return httpx.Response(200, json=TRIALS, request=httpx.Request("GET", url))

    def post(self, url, json=None):
        type(self).posted = {"url": url, "json": json}
        return httpx.Response(
            200, json={"status": "queued", "trial_id": "t-1", "trial_ids": ["t-1", "t-2", "t-3", "t-4"]}
        )


def test_launch_pins_the_current_version_and_watch_reports_each_attack(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")

    result = CliRunner().invoke(app, ["adversarial", "t", "-m", "glm", "--watch", "--json"])

    assert result.exit_code == 0, result.output
    body = _Client.posted["json"]
    assert _Client.posted["url"].endswith("/tasks/t/qa/adversarial")
    assert body["expected_version_id"] == "t-v5"
    assert body["execution"] == {"agent": "auto", "model": "glm"}
    assert body["request_id"]
    assert '"result": "MUST FIX"' in result.output  # t-1: replay passed
    assert '"result": "no defect"' in result.output  # t-2: failed, no exploit.sh
    assert '"result": "needs a human"' in result.output  # t-3: passed, replay failed
    assert result.output.count('"result": "MUST FIX"') == 2  # t-4: replay leaked a file
    assert '"mode": "white"' in result.output


def test_watch_stops_on_a_client_error(monkeypatch):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 5, "watch kept retrying a 404"

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(_Client, "trials_status", 404)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")

    result = CliRunner().invoke(app, ["adversarial", "t", "--watch"])

    assert result.exit_code == 1
    assert "404" in result.output
    assert len(sleeps) == 1


def test_attack_result_while_running_and_replaying():
    assert cli.attack_result(_trial(1, status="running"), None) == "running"
    replay = _trial(9, agent=cli.REPLAY_AGENT, status="queued", reward=None)
    assert cli.attack_result(_trial(1, reward=1.0), replay) == "replaying"
    assert cli.attack_result({**_trial(1), "harbor_stage": "cancelled"}, None) == "cancelled"
    assert cli.attack_result(_trial(1, reward=None), None) == "incomplete"


class _Polls:
    """Serves one trial list per poll, and the same panel every time."""

    def __init__(self, *lists):
        self.lists = list(lists)

    def get(self, url, params=None):
        request = httpx.Request("GET", url)
        if url.endswith("/panel"):
            return httpx.Response(200, json=PANEL, request=request)
        return httpx.Response(200, json=self.lists.pop(0), request=request)


def _recent(trial, **pins):
    finished = datetime.now(timezone.utc).isoformat()
    config = {"adversarial": {**trial["harbor_config"]["adversarial"], **pins}}
    return {**trial, "finished_at": finished, "harbor_config": config}


def test_watch_keeps_an_unlisted_attack_live():
    rows = cli._rows(_Polls([TRIALS[0]]), "api", "t", 5, ["t-1", "t-2"])
    assert [row["live"] for row in rows] == [False, True]
    assert rows[1]["result"] == "missing"


def test_watch_waits_for_the_replay_hooks():
    attack = _recent(TRIALS[5])  # t-4
    replay = _recent(TRIALS[6])  # t-11, leaks a file per the panel
    checked = _recent(TRIALS[5], replay_checked_attempt=1)
    recorded = _recent(TRIALS[6], findings_recorded=True)
    polls = _Polls([attack], [checked, replay], [checked, recorded])
    # The replay is not queued yet, then its findings are not recorded yet.
    assert cli._rows(polls, "api", "t", 5, ["t-4"])[0]["live"] is True
    assert cli._rows(polls, "api", "t", 5, ["t-4"])[0]["live"] is True
    row = cli._rows(polls, "api", "t", 5, ["t-4"])[0]
    assert row["live"] is False and row["result"] == "MUST FIX"
