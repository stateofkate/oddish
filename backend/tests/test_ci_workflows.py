"""Execute CI's migration decisions against disposable Git repositories."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def test_migration_database_is_started_only_after_change_detection():
    job = yaml.safe_load((WORKFLOWS / "migration-upgrade-check.yml").read_text())[
        "jobs"
    ]["upgrade"]
    assert "container" not in job
    assert "services" not in job
    steps = job["steps"]
    detect = next(i for i, step in enumerate(steps) if step.get("id") == "changes")
    database = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Start migration database"
    )
    assert detect < database
    assert steps[database]["if"] == "steps.changes.outputs.run == 'true'"


@pytest.mark.parametrize(
    ("changed_path", "delete", "expected"),
    [
        ("frontend/src/page.tsx", False, "false"),
        ("oddish/alembic/versions/new.py", False, "true"),
        ("backend/alembic/versions/new.py", False, "true"),
        ("backend/alembic/versions/old.py", True, "true"),
        (".github/workflows/migration-upgrade-check.yml", False, "true"),
    ],
)
def test_migration_change_detection(tmp_path, changed_path, delete, expected):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CI test",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "CI test",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
    }
    path = tmp_path / changed_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, env=env, check=True)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    if delete:
        path.unlink()
    else:
        path.write_text("after\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "change"], cwd=tmp_path, env=env, check=True
    )
    job = yaml.safe_load((WORKFLOWS / "migration-upgrade-check.yml").read_text())[
        "jobs"
    ]["upgrade"]
    script = next(step["run"] for step in job["steps"] if step.get("id") == "changes")
    output = tmp_path / "output"
    result = subprocess.run(
        [
            "bash",
            "-c",
            script.replace("${{ github.event.pull_request.base.sha }}", base),
        ],
        cwd=tmp_path,
        env={**env, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == f"run={expected}"


def test_unknown_migration_base_fails_instead_of_reporting_no_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    job = yaml.safe_load((WORKFLOWS / "migration-upgrade-check.yml").read_text())[
        "jobs"
    ]["upgrade"]
    script = next(step["run"] for step in job["steps"] if step.get("id") == "changes")
    output = tmp_path / "output"
    result = subprocess.run(
        [
            "bash",
            "-c",
            script.replace("${{ github.event.pull_request.base.sha }}", "0" * 40),
        ],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("bad_tree", ["oddish", "backend"])
@pytest.mark.parametrize("result", ["fork", "empty", "error", "single"])
def test_both_migration_trees_are_checked_and_errors_fail(tmp_path, bad_tree, result):
    for tree in ("oddish", "backend"):
        (tmp_path / tree).mkdir()
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        'tree="${PWD##*/}"\n'
        'echo "$tree" >> "$CHECKED_TREES"\n'
        'if [ "$tree" = "$BAD_TREE" ]; then\n'
        '  case "$HEAD_RESULT" in\n'
        '    fork) printf "first (head)\\nsecond (head)\\n"; exit 0 ;;\n'
        "    empty) exit 0 ;;\n"
        "    error) exit 7 ;;\n"
        "  esac\n"
        "fi\n"
        'echo "only (head)"\n'
    )
    uv.chmod(0o755)
    job = yaml.safe_load((WORKFLOWS / "migration-head-guard.yml").read_text())["jobs"][
        "guard"
    ]
    script = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Assert exactly one head in each migration tree"
    )
    checked = tmp_path / "checked"
    process = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "BAD_TREE": bad_tree,
            "HEAD_RESULT": result,
            "CHECKED_TREES": str(checked),
        },
        capture_output=True,
        text=True,
    )
    assert (process.returncode == 0) == (result == "single"), process.stdout
    assert checked.read_text().splitlines() == ["oddish", "backend"]


@pytest.mark.parametrize(
    "workflow",
    [
        "e2e-cli.yml",
        "e2e-dashboard.yml",
        "e2e-fixtures.yml",
        "load-only-guard.yml",
        "harbor-pin-guard.yml",
        "monitor-contract-guard.yml",
    ],
)
@pytest.mark.parametrize(
    ("event", "base", "head", "head_repo", "should_run"),
    [
        ("pull_request", "main", "staging", "abundant-ai/oddish", False),
        ("pull_request", "main", "staging", "fork/oddish", True),
        ("pull_request", "staging", "feature", "abundant-ai/oddish", True),
        ("pull_request", "main", "hotfix", "abundant-ai/oddish", True),
        ("push", "", "", "", True),
        ("workflow_dispatch", "", "", "", True),
    ],
)
def test_only_same_repository_promotion_skips_e2e(
    workflow, event, base, head, head_repo, should_run
):
    jobs = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]
    values = {
        "github.event_name": event,
        "github.base_ref": base,
        "github.head_ref": head,
        "github.event.pull_request.head.repo.full_name": head_repo,
        "github.repository": "abundant-ai/oddish",
    }
    for job in jobs.values():
        expression = job["if"].strip().replace("||", " or ")
        for name, value in values.items():
            expression = expression.replace(name, repr(value))
        assert eval(expression, {"__builtins__": {}}, {}) is should_run


@pytest.mark.parametrize(
    "workflow",
    [
        "e2e-cli.yml",
        "e2e-dashboard.yml",
        "e2e-fixtures.yml",
        "load-only-guard.yml",
        "harbor-pin-guard.yml",
        "monitor-contract-guard.yml",
    ],
)
def test_main_pushes_do_not_repeat_staging_tests(workflow):
    # PyYAML reads the bare `on:` key as True.
    triggers = yaml.safe_load((WORKFLOWS / workflow).read_text())[True]
    assert triggers["push"]["branches"] == ["staging"]


def test_fixture_suites_run_only_for_frontend_changes():
    fixtures = yaml.safe_load((WORKFLOWS / "e2e-fixtures.yml").read_text())
    expected = ["frontend/**", ".github/workflows/e2e-fixtures.yml"]
    assert fixtures[True]["pull_request"]["paths"] == expected
    assert fixtures[True]["push"]["paths"] == expected
    assert set(fixtures["jobs"]) == {"files", "delivery"}
    dashboard = yaml.safe_load((WORKFLOWS / "e2e-dashboard.yml").read_text())
    assert set(dashboard["jobs"]) == {"dashboard"}


def test_every_fixture_spec_belongs_to_exactly_one_fixture_suite():
    frontend = WORKFLOWS.parents[1] / "frontend"
    delivery = (frontend / "playwright.delivery.config.ts").read_text()
    files = (frontend / "playwright.files.config.ts").read_text()
    assert not (frontend / "playwright.effort.config.ts").exists()
    for spec in (
        "member-picker.spec.ts",
        "task-run-coverage.spec.ts",
        "reasoning-effort.spec.ts",
    ):
        assert f'"{spec}"' in delivery
        assert f'"{spec}"' not in files
