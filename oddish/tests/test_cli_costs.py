"""Tests for the ``oddish costs`` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _invoke(status_code: int, payload: object, args: list[str]):
    import importlib

    costs_module = importlib.import_module("oddish.cli.costs")
    captured: dict = {}

    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

        @property
        def text(self):
            return json.dumps(payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _Resp()

    app = typer.Typer()
    app.command()(costs_module.costs)
    runner = CliRunner()
    with patch("oddish.cli.costs.httpx.Client", _Client):
        with patch("oddish.cli.costs.require_api_key"):
            with patch("oddish.cli.costs.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.costs.get_auth_headers", return_value={}):
                    result = runner.invoke(app, args)
    captured["result"] = result
    return captured


_ORG = {
    "totals": {"cost_usd": 12.5, "trial_count": 40, "user_count": 3, "experiment_count": 5},
    "by_user": [{"key": "u1", "name": "Alice", "email": "a@x.com", "cost_usd": 8.0, "trial_count": 25}],
    "by_model": [{"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd": 10.0, "trial_count": 30}],
    "experiments": [],
}
_USER = {
    "billed_user_id": "u1",
    "name": "Alice",
    "email": "a@x.com",
    "totals": {"cost_usd": 8.0, "trial_count": 25, "task_count": 4},
    "tasks": [
        {
            "task_id": "t1",
            "task_name": "demo",
            "cost_usd": 5.0,
            "trial_count": 10,
            "models": [
                {"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd": 5.0, "trial_count": 10},
            ],
        }
    ],
}


def test_costs_org_breakdown():
    cap = _invoke(200, _ORG, [])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert cap["url"].endswith("/admin/costs")
    assert cap["params"]["window_days"] == 7
    assert "Total spend" in cap["result"].output
    assert "Alice" in cap["result"].output


def test_costs_user_breakdown_with_window():
    cap = _invoke(200, _USER, ["--user", "u1", "--window-days", "30"])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert cap["url"].endswith("/admin/costs/users/u1")
    assert cap["params"]["window_days"] == 30
    assert "Billed spend" in cap["result"].output
    # Per-user view includes a by-model rollup, like the org-wide view.
    assert "By model" in cap["result"].output
    assert "claude-sonnet-4-5" in cap["result"].output


def test_costs_auth_error_exits_nonzero():
    cap = _invoke(403, {"detail": "Admin role required"}, [])
    assert cap["result"].exit_code == 1
    assert "full-scope API key" in cap["result"].output


def test_costs_core_server_404_hint():
    cap = _invoke(404, {"detail": "Not Found"}, [])
    assert cap["result"].exit_code == 1
    assert "hosted Oddish" in cap["result"].output


def test_costs_user_404_mentions_user():
    cap = _invoke(404, {"detail": "User not found"}, ["--user", "u404"])
    assert cap["result"].exit_code == 1
    assert "u404" in cap["result"].output


def test_costs_json_output():
    cap = _invoke(200, _ORG, ["--json"])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert json.loads(cap["result"].output)["totals"]["cost_usd"] == 12.5


def _invoke_routes(routes: dict[str, list[object]], args: list[str]):
    """Fake API answering each path suffix with its queued payloads in order."""
    import importlib

    costs_module = importlib.import_module("oddish.cli.costs")
    calls: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200 if payload is not None else 404
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return json.dumps(self._payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            calls.append((url, dict(params or {})))
            for suffix, payloads in routes.items():
                if url.endswith(suffix):
                    return _Resp(payloads.pop(0))
            return _Resp(None)

    app = typer.Typer()
    app.command()(costs_module.costs)
    with patch("oddish.cli.costs.httpx.Client", _Client), patch(
        "oddish.cli.costs.require_api_key"
    ), patch("oddish.cli.costs.get_api_url", return_value="http://h"), patch(
        "oddish.cli.costs.get_auth_headers", return_value={}
    ), patch(
        "oddish.cli.costs._resolve_task_id", side_effect=lambda c, a, ref: f"id-{ref}"
    ):
        result = CliRunner().invoke(app, args, env={"COLUMNS": "200"})
    return result, calls


def _trial(kind="agent", cost=1.0, **extra):
    return {"kind": kind, "cost_usd": cost, **extra}


def _detail(name, trials, qa_cost=0.0, verifier=0.0):
    return {
        "task": {"name": name, "trials": trials},
        # Server totals: cost_usd sums every trial, analysis trials included.
        "totals": {
            "cost_usd": sum(t["cost_usd"] for t in trials),
            "qa_cost_usd": qa_cost,
            "verifier_cost_usd": verifier,
        },
    }


def test_task_splits_spend_by_type_without_double_counting_qa():
    detail = _detail(
        "alpha",
        [
            _trial(cost=2.0),
            _trial(cost=9.0, origin="imported"),
            _trial("qa", 0.3),
            _trial("audit", 0.2),
            _trial("summarize", 0.1),
        ],
        qa_cost=0.65,  # 0.6 of analysis trials plus a 0.05 judge charge
        verifier=0.4,
    )
    result, _ = _invoke_routes(
        {"/tasks/id-alpha/detail": [detail]}, ["--task", "alpha", "--json"]
    )
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.output)["tasks"]
    assert row["task_id"] == "id-alpha"
    assert row["solver_usd"] == pytest.approx(2.0)
    assert row["qa_usd"] == pytest.approx(0.3)
    assert row["audit_usd"] == pytest.approx(0.2)
    assert row["summary_usd"] == pytest.approx(0.1)
    assert row["judge_other_usd"] == pytest.approx(0.05)
    assert row["verifier_usd"] == pytest.approx(0.4)
    assert row["total_usd"] == pytest.approx(3.05)


def test_experiment_solver_counts_every_version_of_its_own_runs():
    exp = "exp-1"
    routes = {
        # The listing shows one version per task, plus a gathered run (g-1).
        "/tasks": [
            [
                {"id": "a", "trials": [{"id": "a-2"}]},
                {"id": "b", "trials": [{"id": "g-1"}]},
            ]
        ],
        "/tasks/a/detail": [
            _detail(
                "task-a",
                [
                    _trial(cost=1.0, id="a-1", experiment_id=exp),  # older version
                    _trial(cost=0.5, id="a-2", experiment_id=exp),
                    _trial(cost=99.0, id="a-3", experiment_id="other"),
                    _trial("qa", 0.5, id="a-qa", experiment_id="shadow"),
                ],
                qa_cost=0.5,
            )
        ],
        "/tasks/b/detail": [
            _detail(
                "task-b",
                [
                    _trial(cost=3.0, id="g-1", experiment_id="home"),  # gathered
                    _trial(cost=99.0, id="b-2", experiment_id="other"),
                ],
                verifier=1.0,
            )
        ],
    }
    result, calls = _invoke_routes(routes, ["--experiment", exp])
    assert result.exit_code == 0, result.output
    listing = calls[0][1]
    assert listing["experiment_id"] == exp
    assert listing["include_trials"] == "true"
    out = result.output
    assert out.index("task-b") < out.index("task-a")  # sorted by total
    assert "Summaries" not in out  # all-zero columns are hidden
    assert "25%" in out  # a: $0.50 QA of $2.00
    assert "Total: $6.00 (Solver $4.50, QA $0.50, Verifier $1.00) across 2" in out


def test_task_costs_csv():
    detail = _detail("alpha", [_trial(cost=1.0), _trial("qa", 0.5)], qa_cost=0.5)
    result, _ = _invoke_routes(
        {"/tasks/id-alpha/detail": [detail]}, ["-t", "alpha", "--csv"]
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0] == (
        "task_id,task_name,solver_usd,qa_usd,audit_usd,summary_usd,"
        "judge_other_usd,verifier_usd,total_usd"
    )
    assert lines[1] == "id-alpha,alpha,1.0,0.5,0.0,0.0,0.0,0.0,1.5"


def test_task_and_experiment_together_is_rejected():
    result, calls = _invoke_routes({}, ["--task", "a", "--experiment", "e"])
    assert result.exit_code == 1
    assert not calls


def test_user_with_task_or_experiment_is_rejected():
    result, calls = _invoke_routes({}, ["--user", "u1", "--task", "a"])
    assert result.exit_code == 1
    assert not calls
    result, calls = _invoke_routes({}, ["--user", "u1", "--experiment", "e"])
    assert result.exit_code == 1
    assert not calls
