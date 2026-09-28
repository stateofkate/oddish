"""Delivery QA commands submit the captured versions and expose partial failures."""

import copy
import json
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from oddish.cli.delivery import delivery_app


def board():
    return {
        "delivery": {"id": "delivery-1", "name": "batch", "status": "active"},
        "ready": False,
        "frozen": False,
        "tasks": [
            {
                "task_id": f"task-{i}",
                "task_name": f"Task {i}",
                "version_id": f"task-{i}-v2",
                "version": 2,
                "ready": False,
                "pre_trial_status": "success",
                "pre_trial_error": None,
                "verdict_status": "success",
                "verdict_error": None,
                "qa": {"status": "accepted", "detail": "Current QA accepts this task"},
            }
            for i in range(3)
        ],
    }


def invoke(args, payload=None, post=None, json_output=True):
    payload = payload if payload is not None else board()
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            body = (
                [payload["delivery"]] if request.url.path == "/deliveries" else payload
            )
            return httpx.Response(200, json=body)
        if post:
            return post(request)
        return httpx.Response(200, json={"status": "queued"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with (
        patch("oddish.cli.delivery.httpx.Client", return_value=client),
        patch("oddish.cli.delivery.get_auth_headers", return_value={}),
    ):
        result = CliRunner().invoke(
            delivery_app,
            ["qa", *args, "--api", "http://api"] + (["--json"] if json_output else []),
        )
    return result, requests


def test_rerun_pins_all_versions_deduplicates_and_returns_partial_failures():
    payload = board()
    payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))

    def post(request):
        if "task-1/" in request.url.path:
            return httpx.Response(409, json={"detail": "Task version changed"})
        status = "already_running" if "task-2/" in request.url.path else "queued"
        return httpx.Response(200, json={"status": status})

    result, requests = invoke(["rerun", "batch"], payload, post)
    assert result.exit_code == 1, result.output
    output = json.loads(result.stdout)
    assert output["counts"] == {"queued": 1, "error": 1, "already_running": 1}
    assert "Task version changed" in output["tasks"][1]["detail"]
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 3
    for request in posts:
        task_id = request.url.path.split("/")[2]
        assert request.url.path == f"/tasks/{task_id}/qa/full"
        assert json.loads(request.content) == {"expected_version_id": f"{task_id}-v2"}


def test_network_failure_is_unconfirmed_and_other_tasks_still_submit():
    def post(request):
        if "task-1/" in request.url.path:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"status": "queued"})

    result, requests = invoke(["rerun", "batch"], post=post)
    assert result.exit_code == 1
    output = json.loads(result.stdout)
    assert output["counts"] == {"queued": 2, "error": 1}
    assert "Submission unconfirmed" in output["tasks"][1]["detail"]
    assert len([r for r in requests if r.method == "POST"]) == 3


@pytest.mark.parametrize("mode", ["dry", "finalized", "empty"])
def test_no_submission_for_preview_or_invalid_delivery(mode):
    payload = board()
    args = ["rerun", "batch"]
    if mode == "dry":
        args.append("--dry-run")
    elif mode == "finalized":
        payload["delivery"]["status"] = "finalized"
        payload["frozen"] = True
    else:
        payload["tasks"] = []
    result, requests = invoke(args, payload)
    assert result.exit_code == (0 if mode == "dry" else 1)
    assert not any(r.method == "POST" for r in requests)
    if mode == "dry":
        assert json.loads(result.stdout)["counts"] == {"selected": 3}


def test_status_distinguishes_execution_error_rejection_and_waiting():
    payload = board()
    waiting, failed, rejected = payload["tasks"]
    waiting.update(pre_trial_status="running", verdict_status="queued")
    failed.update(
        pre_trial_status="failed",
        pre_trial_error="Audit failed",
        verdict_status="failed",
        verdict_error="Insufficient evidence",
    )
    rejected["qa"] = {"status": "needs_fixes", "detail": "Verifier misses an assertion"}
    result, requests = invoke(["status", "batch"], payload)
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["pre_trial_counts"] == {"running": 1, "failed": 1, "success": 1}
    assert output["post_trial_counts"] == {"queued": 1, "error": 1, "needs_fixes": 1}
    assert output["tasks"][0]["detail"] == "Waiting for source check and existing runs"
    assert output["tasks"][1]["pre_trial_error"] == "Audit failed"
    assert output["ready"] is False
    assert all(r.method == "GET" for r in requests)


def test_status_of_old_frozen_snapshot_does_not_claim_source_check_success():
    payload = board()
    payload["frozen"] = True
    for row in payload["tasks"]:
        del row["pre_trial_status"]
    result, _ = invoke(["status", "batch"], payload)
    output = json.loads(result.stdout)
    assert output["frozen"] is True
    assert output["pre_trial_counts"] == {"unknown": 3}


def test_live_status_keeps_stale_evidence_outdated_despite_successful_execution():
    payload = board()
    payload["tasks"][0]["qa"] = {
        "status": "outdated",
        "detail": "Trials changed since QA",
    }
    result, _ = invoke(["status", "batch"], payload)
    assert json.loads(result.stdout)["post_trial_counts"] == {
        "outdated": 1,
        "accepted": 2,
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_batches_cover_250_unique_tasks_in_stable_order(dry_run):
    payload = board()
    template = payload["tasks"][0]
    payload["tasks"] = [
        {**template, "task_id": f"task-{i:03}", "version_id": f"v-{i:03}"}
        for i in reversed(range(250))
    ]
    payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))
    selected = []
    for offset, size in [(0, 100), (100, 100), (200, 50)]:
        args = ["rerun", "batch", "--limit", "100", "--offset", str(offset)]
        if dry_run:
            args.append("--dry-run")
        result, requests = invoke(args, payload)
        assert result.exit_code == 0, result.output
        output = json.loads(result.stdout)
        assert output["total_tasks"] == 250
        assert output["offset"] == offset
        assert output["limit"] == 100
        assert output["counts"] == {"selected" if dry_run else "queued": size}
        ids = [row["task_id"] for row in output["tasks"]]
        assert ids == [f"task-{i:03}" for i in range(offset, offset + size)]
        selected.extend(ids)
        posts = [request for request in requests if request.method == "POST"]
        assert len(posts) == (0 if dry_run else size)
        for request in posts:
            task_id = request.url.path.split("/")[2]
            assert task_id in ids
            assert json.loads(request.content) == {
                "expected_version_id": task_id.replace("task-", "v-")
            }
        payload["tasks"].reverse()
    assert len(selected) == len(set(selected)) == 250


@pytest.mark.parametrize(
    ("flags", "expected_ids"),
    [
        ([], ["task-0", "task-1", "task-2"]),
        (["--limit", "2"], ["task-0", "task-1"]),
        (["--limit", "100"], ["task-0", "task-1", "task-2"]),
        (["--offset", "1"], ["task-1", "task-2"]),
    ],
)
def test_optional_batch_flags(flags, expected_ids):
    result, requests = invoke(["rerun", "batch", *flags])
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert [row["task_id"] for row in output["tasks"]] == expected_ids
    assert len([r for r in requests if r.method == "POST"]) == len(expected_ids)


@pytest.mark.parametrize(
    "flags", [["--limit", "0"], ["--limit", "-1"], ["--offset", "-1"]]
)
def test_invalid_batch_flags_fail_before_requests(flags):
    result, requests = invoke(["rerun", "batch", *flags])
    assert result.exit_code == 2
    assert requests == []


@pytest.mark.parametrize("offset", [3, 100])
def test_offset_at_or_past_end_submits_nothing(offset):
    result, requests = invoke(["rerun", "batch", "--offset", str(offset)])
    assert result.exit_code == 1
    assert f"Offset {offset} selects no tasks from 3 tasks." in result.output
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("dry_run", [False, True])
def test_text_output_reports_selected_range(dry_run):
    args = ["rerun", "batch", "--limit", "1", "--offset", "1"]
    if dry_run:
        args.append("--dry-run")
    result, _ = invoke(args, json_output=False)
    assert result.exit_code == 0, result.output
    action = "Selected" if dry_run else "Submitting"
    assert f"{action} 2–2 of 3 tasks (task-ID order)." in result.stdout
