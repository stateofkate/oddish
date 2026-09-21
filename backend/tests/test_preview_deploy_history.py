"""Interrupted preparation must not leave the next push trusting a stopped app."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def history():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/find_last_deploys.py"
    )
    spec = importlib.util.spec_from_file_location("find_last_deploys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lookup(history, monkeypatch, recent, conclusion="cancelled"):
    old = [
        {
            "name": "Deploy preview backend",
            "steps": [
                {"name": "Deploy preview backend", "conclusion": "success"},
                {"name": "Prepare preview database", "conclusion": "success"},
            ],
        }
    ]
    responses = iter(
        [
            {
                "workflow_runs": [
                    {"id": 2, "head_sha": "new", "conclusion": conclusion},
                    {"id": 1, "head_sha": "old"},
                ]
            },
            {"jobs": recent},
            {"jobs": old},
        ]
    )
    monkeypatch.setattr(history, "gh_api", lambda path: next(responses))
    return history.find_last_deployed_shas("org/repo", "branch")


@pytest.mark.parametrize(
    "prepare_result,deploy_result",
    [
        ("cancelled", "skipped"),
        ("success", "cancelled"),
        ("success", "skipped"),
        ("success", "failure"),
        ("failure", "skipped"),
        (None, None),
    ],
)
def test_interrupted_run_invalidates_older_backend(
    history, monkeypatch, prepare_result, deploy_result
):
    result = lookup(
        history,
        monkeypatch,
        [
            {
                "steps": [
                    {
                        "name": "Prepare preview database",
                        "status": "completed" if prepare_result else "in_progress",
                        "conclusion": prepare_result,
                    }
                ]
            },
            {
                "name": "Deploy preview backend",
                "conclusion": deploy_result,
                "steps": [],
            },
        ],
    )
    assert "backend_base" not in result
    assert result["backend_recovery"] == "true"
    if prepare_result == "success":
        assert result["migrations_base"] == "new"


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "failure", None])
def test_skipped_backend_requires_successful_run(history, monkeypatch, conclusion):
    result = lookup(
        history,
        monkeypatch,
        [
            {
                "steps": [
                    {
                        "name": "Prepare preview database",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            {"name": "Deploy preview backend", "conclusion": "skipped", "steps": []},
        ],
        conclusion=conclusion,
    )
    if conclusion == "success":
        assert result == {"backend_base": "old", "migrations_base": "new"}
    else:
        assert result == {"backend_recovery": "true", "migrations_base": "new"}


def test_unstarted_preparation_does_not_invalidate_backend(history, monkeypatch):
    result = lookup(
        history,
        monkeypatch,
        [
            {
                "steps": [
                    {
                        "name": "Prepare preview database",
                        "status": "pending",
                        "conclusion": None,
                    },
                ]
            }
        ],
    )
    assert result == {"backend_base": "old", "migrations_base": "old"}


def test_failed_frontend_does_not_invalidate_successful_backend(history, monkeypatch):
    result = lookup(
        history,
        monkeypatch,
        [
            {
                "steps": [
                    {
                        "name": "Prepare preview database",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            {
                "name": "Deploy preview backend",
                "steps": [{"name": "Deploy preview backend", "conclusion": "success"}],
            },
            {"name": "Update Vercel preview", "conclusion": "failure"},
        ],
    )
    assert result == {"backend_base": "new", "migrations_base": "new"}


def test_recovery_forces_deploy_even_after_backend_changes_are_reverted(
    history, monkeypatch, tmp_path
):
    for key, value in {
        "OWNER_REPO": "org/repo",
        "EVENT_ACTION": "synchronize",
        "HEAD_REF": "branch",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(history, "compute_changed", lambda *a: "false")
    monkeypatch.setattr(
        history, "find_last_deployed_shas", lambda *a: {"backend_recovery": "true"}
    )
    history.main()
    assert "backend_changed=true\n" in (tmp_path / "out").read_text()
