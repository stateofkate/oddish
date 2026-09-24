"""Scheduled cleanup keeps open PR data and removes only verified Neon children."""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/preview/prune_stale_neon_previews.py"
)
spec = importlib.util.spec_from_file_location("preview_neon_prune", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)
MAIN = {"id": "br-main", "name": "main", "default": True}


def _branch(number, days=10, **changes):
    return {
        "id": f"br-preview-{number}",
        "name": f"pr-{number}",
        "parent_id": "br-main",
        "default": False,
        "created_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
        **changes,
    }


class Client:
    def __init__(self, branches, order):
        self.branches = branches
        self.order = order
        self.current = {b["id"]: b for b in branches}

    def find_branch(self, name):
        assert name == "main"
        return MAIN

    def request(self, method, path, **kwargs):
        if path == "/branches":
            return {"branches": [MAIN, *self.branches]}
        self.order.append((method, path))
        if method == "GET":
            current = self.current[path.rsplit("/", 1)[-1]]
            if isinstance(current, Exception):
                raise current
            return {"branch": current}
        assert method == "DELETE"
        return {}


def _run(
    monkeypatch,
    branches,
    *,
    state="closed",
    apps=None,
    dry_run=False,
    stop_fails=False,
    current=None,
):
    order = []
    client = Client(branches, order)
    if current:
        client.current.update(current)
    monkeypatch.setattr(prune, "deployed_apps", lambda _: set(apps or []))
    states = iter(state) if isinstance(state, list) else None
    monkeypatch.setattr(prune, "pr_state", lambda _: next(states) if states else state)

    def stop(app, env):
        order.append(("stop", app))
        if stop_fails:
            raise subprocess.CalledProcessError(1, ["gke-teardown"])

    monkeypatch.setattr(prune, "stop_app", stop)
    code = prune.prune(client, environment="preview", age_days=7, dry_run=dry_run)
    return code, order


@pytest.mark.parametrize("state", ["open", "unknown"])
@pytest.mark.parametrize("branches", [[_branch(42, 100)], []])
def test_open_or_unknown_pr_keeps_app_even_when_database_old_or_absent(
    monkeypatch, state, branches
):
    code, order = _run(monkeypatch, branches, state=state, apps=["oddish-pr-42"])
    assert code == 0
    assert order == []


def test_closed_old_branch_is_deleted_after_app_cleanup(monkeypatch):
    code, order = _run(monkeypatch, [_branch(42)], apps=["oddish-pr-42"])
    assert code == 0
    assert order == [
        ("stop", "oddish-pr-42"),
        ("GET", "/branches/br-preview-42"),
        ("DELETE", "/branches/br-preview-42"),
    ]


def test_closed_young_branch_is_kept_but_app_is_stopped(monkeypatch):
    code, order = _run(monkeypatch, [_branch(42, 1)], apps=["oddish-pr-42"])
    assert code == 0
    assert order == [("stop", "oddish-pr-42")]


def test_main_protected_and_other_parent_branches_are_not_deleted(monkeypatch):
    code, order = _run(
        monkeypatch,
        [
            _branch(42, protected=True),
            _branch(43, parent_id="br-other"),
            _branch(44, default=True),
        ],
    )
    assert code == 0
    assert order == []


def test_gke_failure_keeps_app_database_and_reports_failure(monkeypatch):
    code, order = _run(
        monkeypatch, [_branch(42)], apps=["oddish-pr-42"], stop_fails=True
    )
    assert code == 1
    assert order == [("stop", "oddish-pr-42")]


def test_dry_run_changes_nothing(monkeypatch, capsys):
    code, order = _run(monkeypatch, [_branch(42)], apps=["oddish-pr-42"], dry_run=True)
    assert code == 0
    assert order == []
    output = capsys.readouterr().out
    assert "would stop oddish-pr-42" in output
    assert "would delete Neon pr-42" in output


def test_bad_listing_timestamp_fails_before_stopping_any_app(monkeypatch):
    with pytest.raises(ValueError):
        _run(
            monkeypatch,
            [_branch(42), _branch(43, created_at="invalid")],
            apps=["oddish-pr-42"],
        )


def test_reopened_pr_keeps_database(monkeypatch):
    code, order = _run(monkeypatch, [_branch(42)], state=["closed", "open"])
    assert code == 0
    assert order == [("GET", "/branches/br-preview-42")]


def test_concurrent_replacement_is_not_deleted_by_name(monkeypatch):
    code, order = _run(
        monkeypatch, [_branch(42)], current={"br-preview-42": prune.NeonAPIError(404)}
    )
    assert code == 0
    assert order == [("GET", "/branches/br-preview-42")]


def test_branch_promoted_to_default_is_not_deleted(monkeypatch):
    code, order = _run(
        monkeypatch, [_branch(42)], current={"br-preview-42": _branch(42, default=True)}
    )
    assert code == 1
    assert order == [("GET", "/branches/br-preview-42")]


def test_stop_orders_gke_before_app_and_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(
        prune.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    prune.stop_app("oddish-pr-42", "preview")
    assert calls[0][0].endswith("run_gke_teardown.sh")
    assert calls[1][:3] == ["modal", "app", "stop"]
    assert calls[2][:4] == ["modal", "secret", "delete", "-y"]


@pytest.mark.parametrize("display_keys", [False, True])
def test_modal_json_keys_keep_app_stop_before_database_delete(
    monkeypatch, display_keys
):
    app = (
        {"App ID": "ap-example", "Description": "oddish-pr-42", "State": "deployed"}
        if display_keys
        else {
            "app_id": "ap-example",
            "description": "oddish-pr-42",
            "state": "deployed",
        }
    )
    monkeypatch.setattr(
        prune.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps([app])),
    )
    monkeypatch.setattr(prune, "pr_state", lambda _: "closed")
    order = []
    monkeypatch.setattr(prune, "stop_app", lambda app, env: order.append(("stop", app)))
    client = Client([_branch(42)], order)

    assert prune.prune(client, environment="preview", age_days=7, dry_run=False) == 0
    assert order == [
        ("stop", "oddish-pr-42"),
        ("GET", "/branches/br-preview-42"),
        ("DELETE", "/branches/br-preview-42"),
    ]


@pytest.mark.parametrize("apps", [[{}], [{"Description": "oddish-pr-42"}], [None]])
def test_invalid_modal_entries_block_all_cleanup(monkeypatch, apps):
    monkeypatch.setattr(
        prune.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(apps)),
    )
    order = []
    client = Client([_branch(42)], order)
    with pytest.raises(prune.NeonError, match="invalid app entry"):
        prune.prune(client, environment="preview", age_days=7, dry_run=False)
    assert order == []


def test_list_follows_neon_pagination(monkeypatch):
    client = Client([], [])
    replies = iter(
        [
            {"branches": [_branch(42)], "pagination": {"next": "next-page"}},
            {"branches": [_branch(43)]},
        ]
    )
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: next(replies))
    assert set(prune.list_previews(client, MAIN)) == {"pr-42", "pr-43"}
