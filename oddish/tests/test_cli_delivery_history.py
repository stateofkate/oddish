"""``oddish delivery inventory / import-history / import-receipts`` against a fake API."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from unittest.mock import patch

from typer.testing import CliRunner

from oddish.cli.delivery import delivery_app
from oddish.core.ingest.delivery_backfill import digest

PLAN = {"schema_version": "oddish-delivery-backfill-plan-v1", "org_id": "org-1"}
INVENTORY = {
    "schema_version": "oddish-task-inventory-v1",
    "org_id": "org-1",
    "tasks": [],
}


def _receipt(**kw) -> dict:
    receipt = {
        "id": "r1",
        "org_id": "org-1",
        "plan_schema": PLAN["schema_version"],
        "plan_hash": digest(PLAN),
        "mode": "preview",
        "outcome": "previewed",
        "rejection_reason": None,
        "source_as_of": "2026-09-10",
        "source_revision": None,
        "inventory_captured_at": None,
        "input_hashes": {},
        "summary": {
            "resolved_profiles": 1,
            "unresolved_profiles": 2,
            "category_conflicts_unresolved": 0,
            "source_records": {"created": 4, "updated": 0, "unchanged": 0},
            "aliases": {"created": 1, "updated": 0},
            "assertions": {"created": 1, "updated": 0},
            "delivery_history": {
                "created": 1,
                "updated": 0,
                "skipped_unresolved": 2,
                "customer_labels_unmapped": ["meta"],
            },
        },
        "created_by_user_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt.update(kw)
    return receipt


class _Resp:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def _run(args: list[str], responses: dict[tuple[str, str], object]):
    """``responses`` maps (method, path) to a payload; every call is recorded."""
    calls: list[dict] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def request(self, method, url, **kwargs):
            path = url.replace("http://api", "")
            files = kwargs.get("files") or {}
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "data": kwargs.get("data"),
                    "params": kwargs.get("params"),
                    "json": kwargs.get("json"),
                    "files": {k: v[0] for k, v in files.items()},
                }
            )
            return _Resp(200, responses[(method, path)])

    with (
        patch("oddish.cli.delivery.httpx.Client", _Client),
        patch("oddish.cli.delivery.get_api_url", return_value="http://api"),
        patch("oddish.cli.delivery.get_auth_headers", return_value={}),
    ):
        result = CliRunner().invoke(delivery_app, args)
    return result, calls


def _files(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(INVENTORY))
    return plan, inventory


def test_inventory_writes_a_new_file_and_refuses_to_overwrite(tmp_path):
    output = tmp_path / "inventory.json"
    result, calls = _run(
        ["inventory", "--output", str(output)],
        {("GET", "/deliveries/task-inventory"): INVENTORY},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text()) == INVENTORY
    assert "0 task identities for organization org-1" in result.output
    result, calls = _run(["inventory", "--output", str(output)], {})
    assert result.exit_code == 1 and "already exists" in result.output
    assert calls == []


def test_preview_uploads_both_files_without_apply(tmp_path):
    plan, inventory = _files(tmp_path)
    result, calls = _run(
        [
            "import-history",
            str(plan),
            "--inventory",
            str(inventory),
            "--customer",
            "meta=Meta",
        ],
        {("POST", "/deliveries/history-imports"): _receipt()},
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["files"] == {"plan": "plan.json", "inventory": "inventory.json"}
    assert calls[0]["data"] == {"apply": "false", "customer": ["meta=Meta"]}
    assert "preview" in result.output and "previewed" in result.output
    assert "Nothing was written" in result.output
    assert "customer labels without a customer: meta" in result.output


def test_apply_uploads_with_the_apply_flag(tmp_path):
    plan, inventory = _files(tmp_path)
    result, calls = _run(
        ["import-history", str(plan), "--inventory", str(inventory), "--apply"],
        {
            ("POST", "/deliveries/history-imports"): _receipt(
                mode="apply", outcome="applied"
            )
        },
    )
    assert result.exit_code == 0, result.output
    assert [c["method"] for c in calls] == ["POST"]
    assert calls[0]["data"]["apply"] == "true"
    assert "applied" in result.output


def test_rejected_plan_prints_reasons_and_exits_3(tmp_path):
    plan, inventory = _files(tmp_path)
    rejected = _receipt(
        outcome="rejected",
        rejection_reason="task identities changed since the inventory; re-plan",
        summary={"problems": ["task task-1 is now named 'x'"], "problem_count": 3},
    )
    result, _ = _run(
        ["import-history", str(plan), "--inventory", str(inventory)],
        {("POST", "/deliveries/history-imports"): rejected},
    )
    assert result.exit_code == 3
    assert "identities changed" in result.output
    assert "task task-1 is now named" in result.output
    assert "2 more" in result.output


def test_import_receipts_table_and_json():
    receipts = [
        _receipt(),
        _receipt(id="r0", outcome="rejected", rejection_reason="stale"),
    ]
    result, calls = _run(
        ["import-receipts", "--limit", "5"],
        {("GET", "/deliveries/history-imports"): receipts},
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"limit": 5}
    # Rich wraps cells to the terminal width; check the words, not the layout.
    assert "previewed" in result.output and "rejected" in result.output
    assert "resolved" in result.output and "stale" in result.output
    result, _ = _run(
        ["import-receipts", "--json"],
        {("GET", "/deliveries/history-imports"): receipts},
    )
    assert json.loads(result.output)[0]["id"] == "r1"


@pytest.mark.parametrize("command", ["create", "add"])
def test_complete_selection_is_one_atomic_request(tmp_path, command):
    selection = tmp_path / "selection.json"
    ids = [f"task-{n}" for n in range(5000)]
    selection.write_text(json.dumps({"ids": ids, "truncated": False}))
    responses = {
        ("GET", "/deliveries"): [{"id": "d1", "name": "batch"}],
        ("POST", "/deliveries/d1/tasks"): {"added": 5000},
        ("POST", "/deliveries"): {"id": "d1", "name": "batch"},
    }
    args = [command, "batch", "--tasks-file", str(selection), "--json"]
    args += (
        ["--customer", "xai", "--task", "task-0"] if command == "create" else ["task-0"]
    )
    result, calls = _run(args, responses)
    assert result.exit_code == 0, result.output
    writes = [call for call in calls if call["method"] == "POST"]
    assert len(writes) == 1
    assert writes[0]["json"]["task_ids"] == ids
    assert json.loads(result.output) == (
        {"id": "d1", "name": "batch"} if command == "create" else {"added": 5000}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"ids": ["t1"], "truncated": True},
        {"ids": ["t1"]},
        {"ids": "t1", "truncated": False},
        {"ids": [None], "truncated": False},
        {"ids": [" "], "truncated": False},
        {"ids": [str(n) for n in range(5001)], "truncated": False},
        [],
    ],
)
@pytest.mark.parametrize("command", ["create", "add"])
def test_invalid_selection_never_creates_partial_delivery(tmp_path, payload, command):
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(payload))
    args = [command, "batch", "--tasks-file", str(selection)]
    if command == "create":
        args += ["--customer", "xai"]
    result, calls = _run(args, {})
    assert result.exit_code != 0
    assert calls == []


def test_add_requires_tasks():
    result, calls = _run(["add", "batch"], {})
    assert result.exit_code != 0
    assert calls == []


def test_customer_commands():
    customer = {"id": "c1", "name": "New Lab"}
    result, calls = _run(
        ["create-customer", "New Lab", "--json"], {("POST", "/customers"): customer}
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["json"] == {"name": "New Lab"}
    assert json.loads(result.output) == customer
    result, calls = _run(["customers", "--json"], {("GET", "/customers"): [customer]})
    assert result.exit_code == 0
    assert json.loads(result.output) == [customer]
