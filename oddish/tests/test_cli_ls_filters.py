"""Tests for the full browse-filter flags on ``oddish ls``.

Mirrors ``test_cli_ls_tags.py``: fakes the HTTP client, invokes the command, and
asserts the CLI serializes flags into the exact query params the
``GET /tasks/browse`` endpoint expects.
"""

from __future__ import annotations

import json
import sys

import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _invoke(args: list[str], payload=None, *, json_output=True) -> dict:
    """Run ``ls`` with ``args`` against a fake client; return the captured params."""
    import importlib

    ls_module = importlib.import_module("oddish.cli.ls")
    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return (
                payload
                if payload is not None
                else {"items": [], "has_more": False, "limit": 25, "offset": 0}
            )

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params):
            captured["request_count"] = captured.get("request_count", 0) + 1
            captured["url"] = url
            captured["params"] = dict(params)
            return _FakeResponse()

        def post(self, url, json):
            captured["post_url"] = url
            captured["post_body"] = json
            class SavedResponse:
                status_code = 200
                def json(self):
                    return {"id": "saved-123", **captured["post_body"]}
            return SavedResponse()

    app = typer.Typer()
    app.command()(ls_module.ls)

    runner = CliRunner()
    with patch("oddish.cli.ls.httpx.Client", _FakeClient):
        with patch("oddish.cli.ls.require_api_key"):
            with patch("oddish.cli.ls.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.ls.get_auth_headers", return_value={}):
                    result = runner.invoke(
                        app, [*args, *(["--json"] if json_output else [])]
                    )
    captured["exit_code"] = result.exit_code
    captured["output"] = result.output
    return captured


def test_ls_csv_multiselects_join_repeated_values():
    captured = _invoke(
        [
            "--status",
            "COMPLETED",
            "--status",
            "RUNNING",
            "--agent",
            "cursor-cli",
            "--model",
            "gpt-5",
            "--agent-model",
            "cursor-cli:gpt-5",
            "--provider",
            "openai",
            "--environment",
            "swe",
            "--trial-status",
            "SUCCESS",
            "--origin",
            "oddish",
            "--analysis-classification",
            "flaky",
            "--experiment-id",
            "exp_1",
            "--harbor-sha",
            "abc123",
            "--harbor-stage",
            "build",
            "--priority",
            "HIGH",
            "--verdict-status",
            "SUCCESS",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["statuses"] == "COMPLETED,RUNNING"
    assert p["agents"] == "cursor-cli"
    assert p["models"] == "gpt-5"
    assert p["agent_models"] == "cursor-cli:gpt-5"
    assert p["providers"] == "openai"
    assert p["environments"] == "swe"
    assert p["trial_statuses"] == "SUCCESS"
    assert p["origins"] == "oddish"
    assert p["analysis_classifications"] == "flaky"
    assert p["experiment_ids"] == "exp_1"
    assert p["harbor_shas"] == "abc123"
    assert p["harbor_stages"] == "build"
    assert p["priorities"] == "HIGH"
    assert p["verdict_statuses"] == "SUCCESS"


def test_ls_tristate_booleans_serialize_true_false():
    captured = _invoke(
        [
            "--has-link",
            "--no-has-error",
            "--has-trajectory",
            "--trial-is-probe",
            "--no-run-analysis",
            "--run-probe",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["has_link"] == "true"
    assert p["has_error"] == "false"
    assert p["has_trajectory"] == "true"
    assert p["trial_is_probe"] == "true"
    assert p["run_analysis"] == "false"
    assert p["run_probe"] == "true"


def test_ls_omits_unset_booleans():
    captured = _invoke(["--status", "COMPLETED"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    for key in (
        "has_link",
        "has_error",
        "has_trajectory",
        "trial_is_probe",
        "run_analysis",
        "run_probe",
    ):
        assert key not in p


def test_ls_numeric_ranges_and_sort():
    captured = _invoke(
        [
            "--min-tokens",
            "100",
            "--max-tokens",
            "5000",
            "--reward-min",
            "0.5",
            "--avg-score-min",
            "80",
            "--runtime-total-max",
            "120.5",
            "--total-trials-min",
            "3",
            "--pass-rate-min",
            "50",
            "--sort",
            "avg_score_desc",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["min_tokens"] == 100
    assert p["max_tokens"] == 5000
    assert p["reward_min"] == 0.5
    assert p["avg_score_min"] == 80
    assert p["runtime_total_max"] == 120.5
    assert p["total_trials_min"] == 3
    assert p["pass_rate_min"] == 50
    assert p["sort"] == "avg_score_desc"


def test_ls_compare_and_top_and_or_groups():
    or_groups = '[{"agents": ["cursor-cli"], "reward_min": 0.5}]'
    captured = _invoke(
        [
            "--compare-by",
            "agent",
            "--compare-a",
            "cursor-cli",
            "--compare-b",
            "gemini-cli",
            "--compare-metric",
            "reward",
            "--compare-agg",
            "best",
            "--compare-margin",
            "5",
            "--compare-margin-unit",
            "pct",
            "--top-by",
            "model",
            "--top-value",
            "gpt-5",
            "--top-metric",
            "reward",
            "--or-groups",
            or_groups,
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["compare_by"] == "agent"
    assert p["compare_a"] == "cursor-cli"
    assert p["compare_b"] == "gemini-cli"
    assert p["compare_metric"] == "reward"
    assert p["compare_agg"] == "best"
    assert p["compare_margin"] == 5
    assert p["compare_margin_unit"] == "pct"
    assert p["top_by"] == "model"
    assert p["top_value"] == "gpt-5"
    assert p["top_metric"] == "reward"
    assert p["or_groups"] == or_groups


def test_ls_compare_defaults_when_only_pair_given():
    # A distinct A/B pair with no --compare-by/-metric/-agg must still forward the
    # defaulted trio (else the backend silently skips the comparison).
    captured = _invoke(["--compare-a", "cursor-cli", "--compare-b", "gemini-cli"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["compare_by"] == "agent"
    assert p["compare_metric"] == "reward"
    assert p["compare_agg"] == "best"
    assert p["compare_a"] == "cursor-cli"
    assert p["compare_b"] == "gemini-cli"
    # No margin flag → no margin params.
    assert "compare_margin" not in p
    assert "compare_margin_unit" not in p


def test_ls_compare_omitted_when_pair_incomplete_or_equal():
    # Lone A (no B) emits nothing — mirrors the UI dropping an incomplete pair.
    lone = _invoke(["--compare-a", "cursor-cli"])
    assert lone["exit_code"] == 0
    assert not any(k.startswith("compare_") for k in lone["params"])

    # Identical A == B is not a comparison; emit nothing.
    same = _invoke(["--compare-a", "cursor-cli", "--compare-b", "cursor-cli"])
    assert same["exit_code"] == 0
    assert not any(k.startswith("compare_") for k in same["params"])


def test_ls_top_defaults_when_only_value_given():
    captured = _invoke(["--top-value", "gpt-5"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["top_by"] == "agent"
    assert p["top_metric"] == "reward"
    assert p["top_value"] == "gpt-5"


def test_ls_top_omitted_without_value():
    # --top-by/-metric without --top-value must not emit a partial top block.
    captured = _invoke(["--top-by", "model", "--top-metric", "reward"])
    assert captured["exit_code"] == 0
    assert not any(k.startswith("top_") for k in captured["params"])


def test_ls_created_within_resolves_to_created_after():
    captured = _invoke(["--created-within", "24h"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert "created_after" in p
    assert "created_within" not in p


def test_ls_created_within_rejects_unknown_token():
    captured = _invoke(["--created-within", "bogus"])
    assert captured["exit_code"] != 0


def test_delivery_picker_filters_and_authors():
    captured = _invoke(
        [
            "--delivered-to",
            "xai",
            "--delivered-to",
            "TML",
            "--not-delivered-to",
            "GDM",
            "--has-delivery",
            "--category",
            "swe",
            "--qa-outcome",
            "accepted",
            "--exclude-delivery-id",
            "delivery-1",
            "--author",
            "me",
            "--pin-author",
            "alice",
            "--steps-p50-min",
            "0",
            "--steps-p50-max",
            "200",
            "--agent-count-min",
            "3",
            "--trial-finished-after",
            "2026-09-01",
            "--trial-finished-before",
            "2026-09-20",
            "--sort",
            "agent_count_desc",
        ]
    )
    assert captured["exit_code"] == 0, captured["output"]
    assert captured["params"] == {
        "limit": 25,
        "offset": 0,
        "delivered_to": "xai,TML",
        "not_delivered_to": "GDM",
        "never_delivered": "false",
        "categories": "swe",
        "qa_outcomes": "accepted",
        "exclude_delivery_id": "delivery-1",
        "author": "me",
        "pin_author": "alice",
        "steps_p50_min": 0,
        "steps_p50_max": 200,
        "agent_count_min": 3,
        "trial_finished_after": "2026-09-01T00:00:00",
        "trial_finished_before": "2026-09-20T00:00:00",
        "sort": "agent_count_desc",
    }
    assert "never_delivered" not in _invoke([])["params"]
    assert _invoke(["--never-delivered"])["params"]["never_delivered"] == "true"


@pytest.mark.parametrize(
    "args,payload,expected",
    [
        (["--count", "--delivered-to", "xai"], {"total": 234}, "234"),
        (
            ["--ids", "--not-delivered-to", "GDM"],
            {"ids": ["task-1", "task-2"], "truncated": False},
            "task-1\ntask-2",
        ),
    ],
)
def test_selection_output_modes(args, payload, expected):
    result = _invoke(args, payload, json_output=False)
    assert result["exit_code"] == 0, result["output"]
    assert result["output"].strip() == expected
    assert result["params"]["count_only" if "--count" in args else "ids_only"] == "true"
    assert json.loads(_invoke(args, payload)["output"]) == payload


def test_truncated_ids_require_explicit_json_inspection():
    payload = {"ids": ["task-1"], "truncated": True}
    result = _invoke(["--ids"], payload, json_output=False)
    assert result["exit_code"] == 1
    assert "task-1" not in result["output"]
    assert json.loads(_invoke(["--ids"], payload)["output"]) == payload


def test_filter_options_include_unmapped_labs():
    payload = {
        "delivery_customers": ["xai", "unmapped lab"],
        "categories": ["swe"],
        "agent_models": [{"agent": "oracle", "model": None}],
    }
    result = _invoke(["--filter-options"], payload, json_output=False)
    assert result["exit_code"] == 0, result["output"]
    assert result["url"] == "http://localhost/tasks/browse/facets"
    assert result["params"] == {}
    assert "unmapped lab" in result["output"]
    assert json.loads(_invoke(["--filter-options"], payload)["output"]) == payload


@pytest.mark.parametrize(
    "args",
    [
        ["--ids", "--count"],
        ["--filter-options", "--delivered-to", "xai"],
        ["--count", "--delivery-history"],
        ["--steps-p50-min", "-1"],
        ["--agent-count-min", "0"],
    ],
)
def test_invalid_browse_modes_do_not_send_requests(args):
    result = _invoke(args)
    assert result["exit_code"] != 0
    assert "url" not in result


def test_history_details_use_browse_response_without_extra_requests():
    payload = {
        "items": [
            {
                "id": "t1",
                "name": "task",
                "deliveries": [
                    {
                        "customer": "xai",
                        "batch": "batch-1",
                        "date": "2026-09-01",
                        "source": "history",
                    },
                    {
                        "customer": "xai",
                        "batch": "batch-2",
                        "date": "2026-09-02",
                        "source": "delivery",
                    },
                ],
            }
        ],
        "has_more": False,
    }
    result = _invoke(["--delivery-history"], payload, json_output=False)
    assert result["exit_code"] == 0, result["output"]
    for value in (
        "Sent to",
        "xai",
        "batch-1",
        "batch-2",
        "2026-09-01",
        "history",
        "delivery",
    ):
        assert value in result["output"]
    assert json.loads(_invoke([], payload)["output"]) == payload


@pytest.mark.parametrize(
    "flag,param",
    [
        ("--created-within", "created_after"),
        ("--trial-finished-within", "trial_finished_after"),
    ],
)
def test_rolling_date_windows(flag, param):
    from datetime import datetime, timedelta, timezone

    before = datetime.now(timezone.utc) - timedelta(days=90)
    result = _invoke([flag, "90d"])
    after = datetime.now(timezone.utc) - timedelta(days=90)
    assert result["exit_code"] == 0
    assert before <= datetime.fromisoformat(result["params"][param]) <= after
    assert _invoke([flag, "invalid"])["exit_code"] != 0


def test_selection_reference_is_forwarded():
    result = _invoke(["--selection-id", "saved-123", "--steps-p50-min", "100"])
    assert result["exit_code"] == 0, result["output"]
    assert result["params"]["selection_id"] == "saved-123"
    assert result["params"]["steps_p50_min"] == 100


def test_truncated_selection_is_not_shared():
    result = _invoke(["--share-selection", "Long horizon"], {"ids": ["task"], "truncated": True})
    assert result["exit_code"] == 1
    assert result["request_count"] == 1


def test_share_selection_writes_complete_ids_once():
    ids = [f"task-{i}" for i in range(5000)]
    result = _invoke(["--steps-p50-min", "100", "--share-selection", "Long horizon"], {"ids": ids, "truncated": False})
    assert result["exit_code"] == 0, result["output"]
    assert result["params"]["ids_only"] == "true"
    assert result["params"]["steps_p50_min"] == 100
    assert result["post_url"] == "http://localhost/tag-filters"
    assert result["post_body"] == {"name": "Long horizon", "visibility": "ORG", "filter_ast": {"v": 2, "task_ids": ids}}
    assert json.loads(result["output"])["id"] == "saved-123"
