"""``oddish qa evals`` and ``oddish qa goldens`` against mocked API
responses: what each command sends, prints and links."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import get_args

import httpx
import pytest
from typer.testing import CliRunner

from oddish import schemas
from oddish.cli import app, qa_api
from oddish.cli.config import get_dashboard_url

PAGE = "https://app.example/admin?tab=qa-evals"
SEARCH_FIXTURE = (
    Path(__file__).parents[2] / "frontend" / "tests" / "fixtures" / "task-search.json"
)


def _run(id_: str, **fields) -> dict:
    return {
        "experiment_id": id_,
        "experiment_name": f"{id_} name",
        "title": f"{id_} prompt",
        "created_at": "2026-09-24T10:00:00Z",
        "model": "claude-sonnet-5",
        "model_name": "claude-sonnet-5",
        "prompt_sha256": "a" * 64,
        "subset": False,
        "charted": True,
        "replays": 4,
        "with_golden": 4,
        "judged": 4,
        "correct": 3,
        "errored": 0,
        "pending": 0,
        "accuracy": 0.75,
        "cost_usd": 2.0,
        "cost_per_trial": 0.5,
        "cost_missing": 0,
        **fields,
    }


def _version(sha: str, **fields) -> dict:
    return {
        "sha256": sha,
        "name": f"prompt {sha[:2]}",
        "author": "ana",
        "first_seen_at": "2026-09-20T00:00:00Z",
        "git_commit": None,
        "text_available": True,
        "is_production": False,
        "previous_sha256": None,
        "runs": 0,
        "latest_run_id": None,
        **fields,
    }


STATUS = {
    "execution_configurable": True,
    "replayable": True,
    "production_prompt_sha256": "b" * 64,
    "goldens": {
        "counted": 4,
        "waiting": 1,
        "stale": 2,
        "labels": [
            {"label": "BAD_FAILURE", "count": 3},
            {"label": "GOOD_FAILURE", "count": 1},
        ],
    },
    "cost_per_trial": 0.5,
    "spend": {
        "total_usd": 3.5,
        "trials": 7,
        "cost_missing": 1,
        "by_model": [
            {"model_name": "claude-sonnet-5", "cost_usd": 2.0},
            {"model_name": "claude-haiku-4-5", "cost_usd": 1.5},
        ],
    },
    "runs": [
        _run("run-old", model_name="claude-haiku-4-5", cost_usd=1.5),
        _run("run-new", prompt_sha256="b" * 64, pending=1),
    ],
    "production_run_id": "run-new",
    "frontier": ["run-old"],
    "models": [
        {"model_name": "claude-sonnet-5", "runs": 1, "latest_run_id": "run-new"},
        {"model_name": "claude-haiku-4-5", "runs": 1, "latest_run_id": "run-old"},
    ],
    "versions": [
        _version("a" * 64, runs=1, latest_run_id="run-old"),
        _version("ab" + "0" * 62, text_available=False),
        _version(
            "b" * 64,
            is_production=True,
            previous_sha256="a" * 64,
            runs=1,
            latest_run_id="run-new",
        ),
    ],
}


def _case(trial: str, group: str, outcome: str, golden: bool = True) -> dict:
    return {
        "qa_eval_trial_id": f"qa-{trial}",
        "task_id": "task-1",
        "source_trial_id": trial,
        "original_trial_id": trial,
        "group": group,
        "outcome": outcome,
        "golden_id": f"g-{trial}" if golden else None,
        "expected": "BAD_FAILURE" if golden else None,
        "got": "GOOD_FAILURE" if outcome == "wrong" else None,
        "cost_usd": 0.5,
        "error": None,
    }


RUN = {
    "summary": _run("run-old", errored=1, cost_missing=1),
    "error_groups": [{"message": "Timeout reading <path>", "count": 1}],
    "cases": [
        _case("t-wrong-1", "wrong", "wrong"),
        _case("t-right", "right", "right"),
        _case("t-wrong-2", "wrong", "invalid"),
        _case("t-errored", "not_judged", "errored"),
        _case("t-unlabeled", "no_golden", "off", golden=False),
    ],
}

CREATED = {
    "experiment_id": "run-started",
    "experiment_name": "qa-eval",
    "prompt_sha256": "c" * 64,
    "model": "claude-sonnet-5",
    "trials": [{}, {}],
    "skipped": ["t-gone: trial deleted"],
}


def _golden(id_: str, trial: str, state: str = "counted", **fields) -> dict:
    return {
        "id": id_,
        "expected": {"classification": "BAD_FAILURE"},
        "state": state,
        "source": "labels",
        "source_trial_id": trial,
        "task_version_id": "tv-1",
        "note": "",
        **fields,
    }


def _queued(trial: str, group: str, reward: float, golden: dict | None) -> dict:
    return {
        "trial_id": trial,
        "task_id": "task-1",
        "task_name": f"task-{trial}",
        "reward": reward,
        "group": group,
        "qa_classification": "BAD_FAILURE",
        "qa_subtype": "Timeout" if group == "failed" else None,
        "qa_reason": "Ran out of time.",
        "golden": golden,
    }


QUEUE = {
    "title": "sonnet sweep",
    "trials": [
        _queued("t-1", "failed", 0.0, _golden("g-1", "t-1", "waiting")),
        _queued("t-2", "failed", 0.0, None),
        _queued("t-3", "passed", 1.0, None),
        _queued("t-4", "labeled", 0.0, _golden("g-4", "t-4")),
    ],
}


@pytest.fixture
def api(monkeypatch):
    """Runs ``oddish qa ARGS`` against ``routes``: {(method, path): body},
    where a body of ``(status, json)`` is an error response and a callable
    body is called with the request."""
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example")
    monkeypatch.setenv("ODDISH_DASHBOARD_URL", "https://app.example")
    real_client = httpx.Client
    calls: list[httpx.Request] = []

    def run(routes: dict, args: list[str], **invoke):
        calls.clear()

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            body = routes[(request.method, request.url.path)]
            if callable(body):
                body = body(request)
            if isinstance(body, tuple):
                return httpx.Response(body[0], json=body[1])
            return httpx.Response(204 if body is None else 200, json=body)

        monkeypatch.setattr(
            "oddish.cli.qa_api.httpx.Client",
            lambda **kwargs: real_client(
                **kwargs, transport=httpx.MockTransport(handle)
            ),
        )
        runner = CliRunner(mix_stderr=invoke.pop("mix_stderr", True))
        return runner.invoke(app, ["qa", *args], **invoke), calls

    return run


def _sent(calls: list[httpx.Request], method: str) -> list[dict]:
    return [json.loads(c.content) for c in calls if c.method == method and c.content]


def _json(result) -> object:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- shared pieces --------------------------------------------------------


def test_cli_literals_match_the_schemas():
    assert get_args(qa_api.QAEvalGroup) == get_args(schemas.QAEvalGroup)
    assert get_args(qa_api.QAGoldenState) == get_args(schemas.QAGoldenState)
    assert get_args(qa_api.QAGoldenReview) == get_args(schemas.QAGoldenReview)
    assert get_args(qa_api.QAGoldenQueueGroup) == get_args(schemas.QAGoldenQueueGroup)


@pytest.mark.parametrize(
    "case", json.loads(SEARCH_FIXTURE.read_text()), ids=lambda case: case["search"]
)
def test_search_parses_like_the_page(case):
    assert asdict(qa_api.parse_task_search(case["search"])) == case["expected"]


@pytest.mark.parametrize(
    ("api_url", "dashboard"),
    [
        (
            "https://abundant-ai-preview--oddish-pr-1921-api.modal.run",
            "https://pr-1921.oddish.app",
        ),
        (
            "https://abundant-ai-preview--oddish-pr-1921-api.modal.run/",
            "https://pr-1921.oddish.app",
        ),
        (
            "https://abundant-ai-staging--oddish-staging-api.modal.run",
            "https://staging.oddish.app",
        ),
        ("https://abundant-ai--api.modal.run", "https://www.oddish.app"),
        ("https://api.oddish.app", "https://www.oddish.app"),
        (
            "https://abundant-ai-preview--oddish-pr-x-api.modal.run",
            "https://www.oddish.app",
        ),
        (None, "https://www.oddish.app"),
    ],
)
def test_dashboard_url_follows_the_api(monkeypatch, api_url, dashboard):
    monkeypatch.delenv("ODDISH_DASHBOARD_URL", raising=False)
    assert get_dashboard_url(api_url) == dashboard
    monkeypatch.setenv("ODDISH_DASHBOARD_URL", "https://app.example/")
    assert get_dashboard_url(api_url) == "https://app.example"


def test_a_preview_api_links_its_preview_page(api, monkeypatch):
    monkeypatch.delenv("ODDISH_DASHBOARD_URL")
    preview = "https://abundant-ai-preview--oddish-pr-7-api.modal.run"
    result, _ = api(
        {("GET", "/qa-evals/status"): STATUS}, ["evals", "status", "--api", preview]
    )
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().endswith("https://pr-7.oddish.app/admin?tab=qa-evals")


def test_an_api_error_exits_1_with_the_servers_detail(api):
    result, _ = api(
        {("GET", "/qa-evals/runs/nope"): (404, {"detail": "run not found"})},
        ["evals", "run", "nope"],
    )
    assert result.exit_code == 1
    assert "GET /qa-evals/runs/nope: 404 run not found" in result.output


# --- evals ------------------------------------------------------------------


def test_status_prints_goldens_runs_models_and_spend(api):
    routes = {("GET", "/qa-evals/status"): STATUS}
    result, calls = api(routes, ["evals", "status"])
    assert result.exit_code == 0, result.output
    assert dict(calls[0].url.params) == {"stage": "post_trial"}
    lines = result.output.splitlines()
    assert lines[0] == (
        "goldens  4 (BAD_FAILURE 3, GOOD_FAILURE 1), 1 waiting for review, 2 stale"
    )
    assert lines[1] == "production prompt bbbbbbbb: 75% (3/4)"
    # Newest first; the frontier run is marked.
    assert lines[2].startswith(
        "run-new  2026-09-24  bbbbbbbb run-new prompt  claude-sonnet-5"
    )
    assert "1 running" in lines[2] and "frontier" not in lines[2]
    assert lines[3].endswith("$1.50  frontier")
    assert lines[4] == (
        "model claude-sonnet-5  75% (3/4)  $0.50/trial  1 runs  latest run-new"
    )
    assert lines[6] == (
        "spend  $3.50 over 7 trials (claude-sonnet-5 $2.00, claude-haiku-4-5 $1.50)"
        ", 1 missing a cost"
    )
    assert lines[7] == PAGE

    result, _ = api(routes, ["evals", "status", "--stage", "pre_trial", "--json"])
    assert _json(result) == STATUS


def test_run_lists_one_group_and_links_the_same_view(api):
    routes = {("GET", "/qa-evals/runs/run-old"): RUN}
    result, _ = api(routes, ["evals", "run", "run-old", "--filter", "wrong"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        "run-old name: 75% (3/4), 1 errored, 0 running, $2.00 (1 missing a cost)"
    )
    # The group counts cover every case; only the filtered group is listed.
    assert lines[1] == "wrong 2 · right 1 · not_judged 1 · no_golden 1 = 5 cases"
    assert lines[2:4] == [
        "wrong  t-wrong-1  expected BAD_FAILURE  got GOOD_FAILURE",
        "wrong  t-wrong-2  expected BAD_FAILURE  got invalid",
    ]
    assert lines[4] == "1 × Timeout reading <path>"
    assert lines[5] == f"{PAGE}&run=run-old&filter=wrong"

    result, _ = api(routes, ["evals", "run", "run-old", "--filter", "right", "--json"])
    assert [c["source_trial_id"] for c in _json(result)["cases"]] == ["t-right"]


def test_run_refuses_an_unknown_group(api):
    result, calls = api({}, ["evals", "run", "run-old", "--filter", "still_wrong"])
    assert result.exit_code == 2
    assert calls == []


def test_versions_lists_newest_first_and_links_the_newest(api):
    routes = {("GET", "/qa-evals/status"): STATUS}
    result, _ = api(routes, ["evals", "versions"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "bbbbbbbb  2026-09-20  1 runs  75% (3/4)  prompt bb production",
        "ab000000  2026-09-20  0 runs  —  prompt ab (no text)",
        "aaaaaaaa  2026-09-20  1 runs  75% (3/4)  prompt aa",
        f"{PAGE}&version={'b' * 64}",
    ]
    result, _ = api(routes, ["evals", "versions", "--json"])
    assert _json(result) == STATUS["versions"]


def _diff_routes() -> dict:
    return {
        ("GET", "/qa-evals/status"): STATUS,
        **{
            ("GET", f"/qa-evals/versions/{v['sha256']}"): {
                "sha256": v["sha256"],
                "text": f"text {v['sha256'][:2]}\n",
            }
            for v in STATUS["versions"]
        },
    }


def test_diff_defaults_to_the_previous_version(api):
    result, _ = api(_diff_routes(), ["evals", "diff", "b"])
    assert result.exit_code == 0, result.output
    assert "-text aa" in result.output and "+text bb" in result.output
    # The default base is the page's default too, so it stays out of the link.
    assert result.output.rstrip().endswith(f"{PAGE}&version={'b' * 64}")


def test_diff_resolves_prefixes_and_prints_json(api):
    result, _ = api(_diff_routes(), ["evals", "diff", "b", "--base", "aa", "--json"])
    assert _json(result) == {
        "sha256": "b" * 64,
        "base_sha256": "a" * 64,
        "diff": (
            "--- prompt@aaaaaaaa\n+++ prompt@bbbbbbbb\n"
            "@@ -1 +1 @@\n-text aa\n+text bb\n"
        ),
    }
    result, _ = api(_diff_routes(), ["evals", "diff", "b", "--base", "aa"])
    assert result.output.rstrip().endswith(
        f"{PAGE}&version={'b' * 64}&base_version={'a' * 64}"
    )


def test_diff_refuses_a_version_without_text(api):
    result, calls = api(_diff_routes(), ["evals", "diff", "b", "--base", "ab"])
    assert result.exit_code == 2
    assert "has no recorded text" in result.output
    assert [c.url.path for c in calls] == ["/qa-evals/status"]


def _start_routes(overrides: dict | None = None) -> dict:
    return {
        ("GET", "/qa-evals/status"): STATUS,
        ("GET", "/qa-evals/runs/run-old"): RUN,
        ("POST", "/qa-evals"): CREATED,
        ("GET", "/qa-evals/runs/run-started"): {"summary": {"preflight_passed": True}, "error_groups": []},
        **(overrides or {}),
    }


def test_start_asks_then_replays_every_golden(api, tmp_path):
    prompt = tmp_path / "candidate.txt"
    prompt.write_text("Classify carefully.")
    args = ["evals", "start", "--prompt", str(prompt)]
    result, calls = api(_start_routes(), args)
    # CliRunner sends no input: declined.
    assert "Replay all 4 golden trials plus 1 check (about $2.50 at $0.50/trial)?" in result.output
    assert result.exit_code == 1
    assert _sent(calls, "POST") == []

    result, calls = api(_start_routes(), [*args, "--name", "try-1"], input="y\n")
    assert result.exit_code == 0, result.output
    assert _sent(calls, "POST")[0]["preflight"] is True
    assert _sent(calls, "POST")[1:] == [
        {
            "stage": "post_trial",
            "model": None,
            "prompt_name": "candidate",
            "prompt_text": "Classify carefully.",
            "goldens": True,
            "name": "try-1",
            "preflight_run_id": "run-started",
        }
    ]
    assert "skipped t-gone: trial deleted" in result.output
    assert "started run-started: 2 trials, prompt cccccccc, model claude-sonnet-5" in (
        result.output
    )
    assert result.output.rstrip().endswith(f"{PAGE}&run=run-started")


def test_start_reruns_a_runs_group_by_version_prefix(api):
    result, calls = api(
        _start_routes(),
        ["evals", "start", "--version", "b", "--rerun", "run-old", "--filter", "wrong"]
        + ["--trial", "t-extra", "-y", "--json"],
        mix_stderr=False,
    )
    # The skipped trials go to stderr, leaving the JSON clean.
    assert _json(result) == CREATED
    assert "skipped t-gone" in result.stderr
    body = _sent(calls, "POST")[-1]
    assert body["prompt_sha256"] == "b" * 64
    assert body["source_trial_ids"] == ["t-extra", "t-wrong-1", "t-wrong-2"]
    assert "goldens" not in body and "prompt_text" not in body


def test_start_without_a_filter_reruns_only_cases_with_a_golden(api):
    result, calls = api(
        _start_routes(),
        ["evals", "start", "--version", "b", "--rerun", "run-old", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert _sent(calls, "POST")[0]["source_trial_ids"] == [
        "t-wrong-1",
        "t-right",
        "t-wrong-2",
        "t-errored",
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--version", "a"], "matches 2 prompt versions"),  # aaaa and ab00
        (["--version", "f"], "matches 0 prompt versions"),
        (["--version", "ab"], "has no recorded text"),
        (
            ["--version", "b", "--rerun", "run-old", "--filter", "no_golden"],
            "no trials",
        ),
    ],
)
def test_start_refuses_before_asking(api, args, message):
    # The version is resolved before the cost prompt, so nothing is asked.
    result, calls = api(_start_routes(), ["evals", "start", *args], input="y\n")
    assert result.exit_code == 2
    assert message in result.output
    assert "Replay" not in result.output
    assert _sent(calls, "POST") == []


def test_start_refuses_a_stage_that_cannot_replay(api):
    routes = _start_routes(
        {("GET", "/qa-evals/status"): {**STATUS, "replayable": False}}
    )
    result, calls = api(
        routes, ["evals", "start", "--version", "b", "--stage", "pre_trial"]
    )
    assert result.exit_code == 2
    assert "cannot be replayed yet" in result.output
    assert _sent(calls, "POST") == []


def test_start_filter_needs_rerun(api):
    result, calls = api({}, ["evals", "start", "--version", "b", "--filter", "wrong"])
    assert result.exit_code == 2
    assert calls == []


# --- goldens ------------------------------------------------------------------


EXPERIMENTS = {
    "experiments": [
        {
            "id": "exp-1",
            "name": "sonnet sweep",
            "author": {"name": "@ana"},
            "last_author": None,
            "last_created_at": "2026-09-24T10:00:00Z",
            "reward_total": 40,
            "reward_success": 28,
            "total_trials": 42,
        }
    ]
}


def test_experiments_lists_yours_by_default(api):
    routes = {("GET", "/dashboard"): EXPERIMENTS}
    result, calls = api(routes, ["goldens", "experiments"])
    assert result.exit_code == 0, result.output
    assert dict(calls[0].url.params) == {
        "include_queues": "false",
        "include_tasks": "false",
        "include_usage": "false",
        "experiments_limit": "25",
        "experiments_author": "me",
    }
    assert result.output.splitlines() == [
        "exp-1  sonnet sweep  @ana  2026-09-24  12 failed / 42 trials",
        f"{PAGE}&label=1",
    ]
    result, _ = api(routes, ["goldens", "experiments", "--json"])
    assert _json(result) == EXPERIMENTS["experiments"]


def test_experiments_search_is_parsed_like_the_page(api):
    routes = {("GET", "/dashboard"): EXPERIMENTS}
    result, calls = api(
        routes,
        [
            "goldens",
            "experiments",
            "--member",
            "all",
            "--search",
            "tag:smoke -tag:x sweep",
        ]
        + ["--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    params = dict(calls[0].url.params)
    assert "experiments_author" not in params
    assert params["experiments_limit"] == "5"
    assert params["experiments_query"] == "sweep"
    assert (params["experiments_tags"], params["experiments_tags_none"]) == (
        "smoke",
        "x",
    )
    assert result.output.rstrip().endswith(
        f"{PAGE}&label=1&member=all&search=tag%3Asmoke+-tag%3Ax+sweep"
    )


def test_experiments_of_another_member_are_an_author_search(api):
    routes = {("GET", "/dashboard"): EXPERIMENTS}
    result, calls = api(
        routes, ["goldens", "experiments", "--member", "ana@example.com"]
    )
    params = dict(calls[0].url.params)
    assert "experiments_author" not in params
    assert params["experiments_author_query"] == "ana@example.com"
    assert result.output.rstrip().endswith(
        f"{PAGE}&label=1&member=all&search=author%3Aana%40example.com"
    )


def test_trials_shows_one_queue_group_and_links_it(api):
    routes = {("GET", "/qa-goldens/trials"): QUEUE}
    result, calls = api(routes, ["goldens", "trials", "sonnet sweep"])
    assert result.exit_code == 0, result.output
    assert dict(calls[0].url.params) == {"experiment": "sonnet sweep"}
    assert result.output.splitlines() == [
        "t-1  task-t-1  reward 0  QA BAD_FAILURE (Timeout)  golden BAD_FAILURE waiting",
        "t-2  task-t-2  reward 0  QA BAD_FAILURE (Timeout)  golden unlabeled",
        "sonnet sweep: failed 2 · passed 1 · labeled 1 · all 4",
        f"{PAGE}&experiment=sonnet+sweep",
    ]

    result, calls = api(
        routes, ["goldens", "trials", "--review", "stale", "--queue", "all"]
    )
    assert dict(calls[0].url.params) == {"review": "stale"}
    assert result.output.rstrip().endswith(f"{PAGE}&review=stale&queue=all")

    result, _ = api(
        routes, ["goldens", "trials", "exp-1", "--queue", "labeled", "--json"]
    )
    assert [t["trial_id"] for t in _json(result)["trials"]] == ["t-4"]


@pytest.mark.parametrize("args", [[], ["exp-1", "--review", "stale"]])
def test_trials_needs_one_experiment_or_review(api, args):
    result, calls = api({}, ["goldens", "trials", *args])
    assert result.exit_code == 2
    assert calls == []


def test_label_sets_a_trials_golden_and_links_its_drawer(api):
    routes = {
        ("PUT", "/qa-goldens/trials/t-1"): _golden("g-1", "t-1"),
        ("GET", "/trials/t-1"): {"id": "t-1", "task_id": "task 1"},
    }
    result, calls = api(routes, ["goldens", "label", "t-1", "BAD_FAILURE"])
    assert result.exit_code == 0, result.output
    assert _sent(calls, "PUT") == [{"classification": "BAD_FAILURE", "note": None}]
    assert result.output.splitlines() == [
        "t-1: BAD_FAILURE (golden g-1)",
        "https://app.example/tasks/task%201?trial=t-1",
    ]

    result, calls = api(
        routes, ["goldens", "label", "t-1", "BAD_FAILURE", "--note", "ok", "--json"]
    )
    assert _sent(calls, "PUT")[0]["note"] == "ok"
    assert _json(result) == _golden("g-1", "t-1")


def test_list_filters_and_shows_each_state(api):
    goldens = [_golden("g-1", "t-1"), _golden("g-2", "t-2", "stale", source=None)]
    routes = {("GET", "/qa-goldens"): goldens}
    result, calls = api(
        routes,
        ["goldens", "list", "--stage", "post_trial", "--waiting", "--trial", "t-1"]
        + ["--trial", "t-2", "--source", "labels"],
    )
    assert result.exit_code == 0, result.output
    assert list(calls[0].url.params.multi_items()) == [
        ("trial", "t-1"),
        ("trial", "t-2"),
        ("stage", "post_trial"),
        ("source", "labels"),
        ("state", "waiting"),
    ]
    assert result.output.splitlines() == [
        "g-1  labels  t-1  BAD_FAILURE",
        "g-2  -  t-2  BAD_FAILURE stale",
        "2 goldens",
        PAGE,
    ]
    result, _ = api(routes, ["goldens", "list", "--json"])
    assert _json(result) == goldens


def test_review_confirms_named_and_waiting_goldens_once(api):
    routes = {
        ("GET", "/qa-goldens"): [
            _golden("g-1", "t-1", "waiting"),
            _golden("g-2", "t-2", "waiting"),
        ],
        ("POST", "/qa-goldens/g-1/review"): _golden("g-1", "t-1"),
        ("POST", "/qa-goldens/g-2/review"): _golden("g-2", "t-2"),
        ("GET", "/trials/t-2"): {"id": "t-2", "task_id": "task-1"},
    }
    result, calls = api(
        routes, ["goldens", "review", "g-1", "--all-waiting", "--source", "labels"]
    )
    assert result.exit_code == 0, result.output
    # The server's waiting state, which leaves stale goldens out.
    assert dict(calls[0].url.params) == {"source": "labels", "state": "waiting"}
    assert [c.url.path for c in calls if c.method == "POST"] == [
        "/qa-goldens/g-1/review",
        "/qa-goldens/g-2/review",
    ]
    assert result.output.splitlines() == ["reviewed 2", PAGE]

    result, _ = api(routes, ["goldens", "review", "g-2", "--json"])
    assert _json(result) == [_golden("g-2", "t-2")]

    # One golden links to its trial's drawer.
    result, _ = api(routes, ["goldens", "review", "g-2"])
    assert result.output.splitlines() == [
        "reviewed 1",
        "https://app.example/tasks/task-1?trial=t-2",
    ]


def test_retire_deletes_each_golden(api):
    routes = {("DELETE", "/qa-goldens/g-1"): None, ("DELETE", "/qa-goldens/g-2"): None}
    result, calls = api(routes, ["goldens", "retire", "g-1", "g-2"])
    assert result.exit_code == 0, result.output
    assert [c.url.path for c in calls] == ["/qa-goldens/g-1", "/qa-goldens/g-2"]
    assert result.output.splitlines() == ["retired 2", PAGE]
    result, _ = api(routes, ["goldens", "retire", "g-1", "--json"])
    assert _json(result) == {"retired": ["g-1"]}


LABELS_CSV = (
    "trial_id,gold_class,batch,note\n"
    "t-new,BAD_FAILURE,a,looks broken\n"
    "t-new-2,GOOD_FAILURE,b,\n"
    "t-same,BAD_FAILURE,a,\n"
    "t-other,GOOD_FAILURE,a,\n"
)


def _import_routes(
    overrides: dict | None = None, copies: dict[str, dict] | None = None
) -> dict:
    """Two stored goldens; asked for one trial, the golden ``copies`` stores
    that copy's label under (the server's copy rule), else none."""
    stored = [
        _golden("g-same", "t-same", "waiting"),
        _golden("g-other", "t-other", "waiting"),
    ]

    def goldens(request: httpx.Request) -> list[dict]:
        trials = request.url.params.get_list("trial")
        if not trials:
            return stored
        return [copies[t] for t in trials if t in (copies or {})]

    return {
        ("GET", "/qa-goldens"): goldens,
        ("POST", "/qa-goldens"): _golden("g-new", "t-new", "waiting"),
        ("POST", "/qa-goldens/g-same/review"): _golden("g-same", "t-same"),
        ("POST", "/qa-goldens/g-new/review"): _golden("g-new", "t-new"),
        **(overrides or {}),
    }


def test_import_adds_new_rows_and_refuses_disagreeing_ones(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text(LABELS_CSV)
    result, calls = api(
        _import_routes(),
        ["goldens", "import", str(labels), "--review-where", "batch=a"],
    )
    assert result.exit_code == 1, result.output
    assert dict(calls[0].url.params) == {"stage": "post_trial"}
    assert _sent(calls, "POST") == [
        {
            "stage": "post_trial",
            "source": "labels",
            "source_trial_id": "t-new",
            "expected": {"classification": "BAD_FAILURE"},
            "note": "looks broken",
        },
        {
            "stage": "post_trial",
            "source": "labels",
            "source_trial_id": "t-new-2",
            "expected": {"classification": "GOOD_FAILURE"},
            "note": "",
        },
    ]
    # Batch a is reviewed: the agreeing golden and the new row, never the
    # disagreeing golden.
    reviews = [c.url.path for c in calls if c.url.path.endswith("/review")]
    assert reviews == ["/qa-goldens/g-same/review", "/qa-goldens/g-new/review"]
    assert (
        "4 rows, 2 already labeled (1 of those to review, 1 differently), 2 to add"
        in (result.output)
    )
    assert "refused t-other: already labeled {'classification': 'BAD_FAILURE'}" in (
        result.output
    )
    assert "added 2, reviewed 2, refused 1" in result.output


def test_import_sends_neither_label_of_rows_that_disagree(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "trial_id,gold_class\n"
        "t-new,BAD_FAILURE\n"
        "t-new,GOOD_FAILURE\n"
        "t-new,BAD_FAILURE\n"
        "t-new-2,GOOD_FAILURE\n"
    )
    result, calls = api(_import_routes(), ["goldens", "import", str(labels)])
    assert result.exit_code == 1, result.output
    # A golden written from the first row would refuse the corrected file.
    assert [body["source_trial_id"] for body in _sent(calls, "POST")] == ["t-new-2"]
    assert "refused t-new: conflicting labels in import" in result.output
    assert "added 1, reviewed 0, refused 1" in result.output


def test_import_dry_run_writes_nothing(api, tmp_path):
    labels = tmp_path / "labels.tsv"
    labels.write_text(LABELS_CSV.replace(",", "\t"))
    args = ["goldens", "import", str(labels), "--where", "batch=a", "--review-all"]
    result, calls = api(_import_routes(), [*args, "--dry-run", "--source", "s1"])
    assert result.exit_code == 0, result.output
    # The goldens, then the one row without a golden, in case it is a copy.
    assert [(c.method, c.url.params.get("trial")) for c in calls] == [
        ("GET", None),
        ("GET", "t-new"),
    ]
    assert (
        "3 rows, 2 already labeled (1 of those to review, 1 differently), 1 to add"
        in (result.output)
    )
    result, _ = api(_import_routes(), [*args, "--dry-run", "--json"], mix_stderr=False)
    assert json.loads(result.stdout) == {
        "add": ["t-new"],
        "review": ["g-same"],
        "refused": ["t-other: already labeled {'classification': 'BAD_FAILURE'}"],
    }


def test_a_rerun_finds_a_copys_golden_under_its_original(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("trial_id,gold_class\nt-copy,BAD_FAILURE\nt-copy-2,BAD_FAILURE\n")
    copies = {
        "t-copy": _golden("g-orig", "t-orig", "waiting"),
        "t-copy-2": _golden("g-orig-2", "t-orig-2", "stale"),
    }
    routes = _import_routes(
        {("POST", "/qa-goldens/g-orig/review"): _golden("g-orig", "t-orig")}, copies
    )
    result, calls = api(routes, ["goldens", "import", str(labels), "--review-all"])
    assert result.exit_code == 0, result.output
    assert _sent(calls, "POST") == []
    # The stale golden is left for someone to check against the changed task.
    reviews = [c.url.path for c in calls if c.url.path.endswith("/review")]
    assert reviews == ["/qa-goldens/g-orig/review"]
    assert (
        "2 rows, 2 already labeled (1 of those to review, 0 differently), 0 to add"
        in result.output
    )


def test_import_json_reports_what_was_added(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("source_trial_id,expected_classification\nt-new,BAD_FAILURE\n")
    result, calls = api(
        _import_routes(),
        ["goldens", "import", str(labels), "--review-all", "--json"],
        mix_stderr=False,
    )
    assert json.loads(result.stdout) == {
        "added": ["g-new"],
        "reviewed": ["g-new"],
        "refused": [],
    }


def test_import_reports_a_refused_post_and_goes_on(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("trial_id,gold_class\nt-new,BAD_FAILURE\n")
    routes = _import_routes(
        {("POST", "/qa-goldens"): (422, {"detail": "not a solver trial"})}
    )
    result, _ = api(routes, ["goldens", "import", str(labels)])
    assert result.exit_code == 1
    assert "refused t-new: 422 not a solver trial" in result.output
    assert "added 0, reviewed 0, refused 1" in result.output


def test_a_request_validation_422_prints_each_field_and_message(api, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("trial_id,gold_class\nt-new,BAD_FAILUR\n")
    # FastAPI's shape for a pydantic error: a list, each loc led by "body".
    detail = [
        {
            "type": "value_error",
            "loc": ["body"],
            "msg": "Value error, expected.classification must be one of [...]",
            "input": {"stage": "post_trial"},
            "ctx": {"error": {}},
        },
        {
            "type": "enum",
            "loc": ["body", "stage"],
            "msg": "Input should be 'pre_trial' or 'post_trial'",
        },
    ]
    routes = _import_routes({("POST", "/qa-goldens"): (422, {"detail": detail})})
    result, _ = api(routes, ["goldens", "import", str(labels)])
    assert result.exit_code == 1
    assert (
        "refused t-new: 422 body: Value error, expected.classification must be "
        "one of [...]; stage: Input should be 'pre_trial' or 'post_trial'"
    ) in result.output


def test_import_of_an_answer_key_adds_pre_trial_goldens(api, tmp_path):
    key = tmp_path / "key.json"
    key.write_text(
        json.dumps(
            {
                "schema_version": "gdm_source_audit_answer_key_v1",
                "label_version": "v2",
                "cases": [
                    {
                        "case_id": "c1",
                        "task_version_id": "tv-9",
                        "expected_task_judgment": "reject",
                        "required_findings": [
                            {
                                "finding_id": "f1",
                                "evidence_references": [
                                    {"path": "/Users/me/x", "repo_path": "tests/a.py"}
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    routes = _import_routes({("GET", "/qa-goldens"): []})
    result, calls = api(routes, ["goldens", "import", str(key)])
    assert result.exit_code == 0, result.output
    assert dict(calls[0].url.params) == {"stage": "pre_trial"}
    assert _sent(calls, "POST") == [
        {
            "stage": "pre_trial",
            "source": "key",
            "task_version_id": "tv-9",
            "expected": {
                "task_judgment": "reject",
                "required_findings": [
                    {
                        "finding_id": "f1",
                        "evidence_references": [{"repo_path": "tests/a.py"}],
                    }
                ],
            },
            "note": "v2 c1",
        }
    ]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("empty.csv", ""),
        ("header.csv", "trial_id,gold_class\n"),
        (
            "key.json",
            '{"schema_version": "gdm_source_audit_answer_key_v1", "cases": []}',
        ),
    ],
)
def test_import_of_an_empty_file_exits_2(api, tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    result, calls = api({}, ["goldens", "import", str(path)])
    assert result.exit_code == 2
    assert "has no rows" in result.output
    assert calls == []


@pytest.mark.parametrize("text", ["", "[]", "{", '{"cases": []}'])
def test_import_of_a_json_file_that_is_no_answer_key_exits_2(api, tmp_path, text):
    path = tmp_path / "key.json"
    path.write_text(text)
    result, calls = api({}, ["goldens", "import", str(path)])
    assert result.exit_code == 2
    assert "expected schema_version" in result.output
    assert calls == []


def test_start_stops_before_full_run_on_failed_preflight(api):
    routes = _start_routes({("GET", "/qa-evals/runs/run-started"): {
        "summary": {"preflight_passed": False, "pending": 0},
        "error_groups": [{"message": "ModelNotFoundError", "count": 1}],
    }})
    result, calls = api(routes, ["evals", "start", "--version", "b", "--model", "openai/gpt-6-sol", "-y"])
    assert result.exit_code == 1
    assert "Full run not submitted" in result.output
    [check] = _sent(calls, "POST")
    assert check["preflight"] is True
    assert check["model"] == "openai/gpt-6-sol"


def test_start_checks_multiple_models_with_shared_execution_settings(api, tmp_path):
    config = tmp_path / "agent.json"
    config.write_text('{"kwargs":{"max_turns":20}}')
    result, calls = api(_start_routes(), [
        "evals", "start", "--version", "b", "-y", "--json", "--agent", "mini-swe-agent",
        "--model", "openai/gpt-6-sol", "--model", "openai/gpt-6-luna",
        "--reasoning-effort", "high", "--agent-config", str(config),
    ], mix_stderr=False)
    assert result.exit_code == 0, result.output
    check_sol, check_luna, sol, luna = _sent(calls, "POST")
    assert check_sol["preflight"] and check_luna["preflight"]
    assert sol["preflight_run_id"] and luna["preflight_run_id"]
    for body in (check_sol, check_luna, sol, luna):
        assert body["agent"] == "mini-swe-agent"
        assert body["agent_config"] == {"kwargs": {"max_turns": 20, "reasoning_effort": "high"}}
    assert sol["model"] == "openai/gpt-6-sol"
    assert luna["model"] == "openai/gpt-6-luna"
    assert len(_json(result)) == 2


def test_start_refuses_old_api_before_it_can_ignore_preflight(api):
    routes = _start_routes({("GET", "/qa-evals/status"): {**STATUS, "execution_configurable": False}})
    result, calls = api(routes, ["evals", "start", "--version", "b", "-y"])
    assert result.exit_code == 2
    assert "execution-configuration update" in result.output
    assert _sent(calls, "POST") == []
