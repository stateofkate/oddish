"""Preview links must follow the database used by the deployed app."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / ".github/scripts/preview"


@pytest.fixture
def links(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "post_preview_links", SCRIPTS / "post_preview_links.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in tuple(os.environ):
        if key.startswith(("PREVIEW_", "NEON_", "VERCEL_", "MODAL_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("PREVIEW_COMMIT_SHA", "abc123456")
    monkeypatch.setenv("VERCEL_PREVIEW_URL", "https://pr-42.oddish.app")
    monkeypatch.setenv("MODAL_API_URL", "https://preview.example")
    monkeypatch.setenv("PREVIEW_BACKEND_LABEL", "oddish-pr-42")
    monkeypatch.setenv("NEON_PROJECT_ID", "project-demo")
    monkeypatch.setenv("NEON_BRANCH_ID", "br-preview")
    return module


def test_comment_links_to_the_pr_neon_database(links, monkeypatch):
    monkeypatch.setenv("NEON_STAGING_BRANCH_ID", "br-staging")
    body = links.build_body()
    assert (
        "[Neon br-preview](https://console.neon.tech/app/projects/project-demo/branches/br-preview)"
        in body
    )
    assert "[https://pr-42.oddish.app](https://pr-42.oddish.app)" in body
    assert "[oddish-pr-42](https://preview.example)" in body
    assert "supabase.com" not in body
    assert "br-staging" not in body
    assert "Plan:" not in body


@pytest.mark.parametrize(
    "name", ["NEON_BRANCH_ID", "MODAL_API_URL", "VERCEL_PREVIEW_URL"]
)
def test_links_reject_missing_or_invalid_preview_targets(links, monkeypatch, name):
    monkeypatch.setenv(name, "")
    monkeypatch.setenv("NEON_STAGING_BRANCH_ID", "br-staging")
    monkeypatch.setenv("STAGING_API_URL", "https://staging.example")
    monkeypatch.setenv("PROD_API_URL", "https://production.example")

    def no_github_request(*_args, **_kwargs):
        pytest.fail("Invalid preview metadata must fail before calling GitHub")

    monkeypatch.setattr(links, "gh_request", no_github_request)
    with pytest.raises(ValueError, match=name):
        links.main()


def test_body_updates_only_the_marked_block(links):
    prefix = "# Fix a bug\n\nKeep the review notes here.\n\n"
    suffix = "\n\n## Checks\nThese are the author's checks.\n"
    original = prefix + links.MARKER + "old links" + links.END_MARKER + suffix
    preview = links.build_body()
    updated = links.update_body(original, preview)
    assert updated == prefix + preview + suffix
    assert links.update_body(updated, preview) == updated
    assert updated.count(links.MARKER) == 1


@pytest.mark.parametrize("existing_comment", [True, False])
def test_main_updates_body_and_keeps_one_sticky_comment(
    links, monkeypatch, existing_comment
):
    writes = []

    def request(method, path, body=None):
        if method == "GET" and path.startswith("/issues/42/comments"):
            return (
                [{"id": 8, "body": links.MARKER + "old Supabase link"}]
                if existing_comment
                else []
            )
        if method == "GET" and path == "/pulls/42":
            return {"body": "Keep this PR description."}
        writes.append((method, path, body))

    monkeypatch.setattr(links, "gh_request", request)
    links.main()
    comment_method = "PATCH" if existing_comment else "POST"
    comment_path = "/issues/comments/8" if existing_comment else "/issues/42/comments"
    assert writes == [
        (comment_method, comment_path, {"body": links.build_body()}),
        (
            "PATCH",
            "/pulls/42",
            {"body": "Keep this PR description.\n\n" + links.build_body()},
        ),
    ]


def test_banner_build_uses_the_pr_database_without_branch_env_writes(
    tmp_path,
):
    bins = tmp_path / "bin"
    bins.mkdir()
    (tmp_path / "frontend").mkdir()
    (bins / "python").symlink_to(sys.executable)
    curl = bins / "curl"
    curl.write_text("#!/bin/sh\nprintf 200\n")
    curl.chmod(0o755)
    script = tmp_path / "update_vercel_preview.sh"
    shutil.copy(SCRIPTS / script.name, script)
    (tmp_path / "redeploy_vercel.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['BUILD_ENV_LOG']).write_text(json.dumps(dict(os.environ)))\n"
        "Path(os.environ['GITHUB_OUTPUT']).write_text('preview_url=https://preview.example\\npreview_alias_url=https://pr-42.oddish.app\\n')\n"
    )
    build_log = tmp_path / "build.json"
    env = {
        "PATH": f"{bins}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "VERCEL_GIT_BRANCH": "feature/demo",
        "VERCEL_TOKEN": "test-token",
        "VERCEL_ORG_ID": "test-team",
        "VERCEL_PROJECT_ID": "test-project",
        "PREVIEW_ALIAS_HOSTNAME": "pr-42.oddish.app",
        "STAGING_API_URL": "https://staging.example",
        "PROD_API_URL": "https://production.example",
        "MODAL_API_URL": "https://api.preview.example",
        "MODAL_APP_NAME": "oddish-pr-42",
        "NEON_PROJECT_ID": "project-demo",
        "NEON_BRANCH_ID": "br-preview",
        "NEON_STAGING_BRANCH_ID": "br-staging",
        "PR_URL": "https://github.com/oddish/oddish/pull/42",
        "PR_TITLE": "Keep preview data",
        "BUILD_ENV_LOG": str(build_log),
    }
    result = subprocess.run(
        ["bash", str(script)], env=env, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    build = json.loads(build_log.read_text())
    expected = "https://console.neon.tech/app/projects/project-demo/branches/br-preview"
    assert build["PREVIEW_DATABASE_URL"] == expected
    assert build["PREVIEW_DATABASE_LABEL"] == "Neon br-preview"
    assert build["PREVIEW_BACKEND_LABEL"] == "oddish-pr-42"
    assert build["BACKEND_API_URL"] == "https://api.preview.example"
    assert (
        "preview_alias_url=https://pr-42.oddish.app\n"
        in (tmp_path / "output").read_text()
    )


@pytest.fixture
def reset_job():
    path = SCRIPTS.parents[1] / "workflows/preview-reset.yml"
    return yaml.safe_load(path.read_text())["jobs"]["reset"]


def _run_reset_guard(
    tmp_path,
    reset_job,
    *,
    state="open",
    repo="owner/repo",
    ref="feature/demo",
    title="Fix previews",
):
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "python").symlink_to(sys.executable)
    payload = tmp_path / "pr.json"
    payload.write_text(
        json.dumps(
            {
                "state": state,
                "head": {"ref": ref, "sha": "abc123", "repo": {"full_name": repo}},
                "title": title,
                "html_url": "https://github.com/owner/repo/pull/42",
            }
        )
    )
    curl = bins / "curl"
    curl.write_text('#!/bin/sh\ncat "$PR_RESPONSE_FILE"\n')
    curl.chmod(0o755)
    script = reset_job["steps"][0]["run"].replace(
        "${{ github.repository }}", "owner/repo"
    )
    env_file = tmp_path / "env"
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bins}:{os.environ['PATH']}",
            "GITHUB_ENV": str(env_file),
            "PR_RESPONSE_FILE": str(payload),
            "GH_TOKEN": "test-token",
            "PR_NUMBER": "42",
            "VERCEL_TOKEN": "test-token",
            "VERCEL_ORG_ID": "test-team",
            "VERCEL_PROJECT_ID": "test-project",
        },
    )
    return result, env_file


def test_reset_metadata_keeps_multiline_title_as_one_value(tmp_path, reset_job):
    title = "Keep data\nNEON_BRANCH_ID=br-wrong\nEOF\n$(echo injected)"
    result, env_file = _run_reset_guard(tmp_path, reset_job, title=title)
    assert result.returncode == 0, result.stderr
    values = {}
    lines = iter(env_file.read_text().splitlines())
    for line in lines:
        key, delimiter = line.split("<<", 1)
        value = []
        for line in lines:
            if line == delimiter:
                break
            value.append(line)
        values[key] = "\n".join(value)
    assert values == {
        "VERCEL_GIT_BRANCH": "feature/demo",
        "VERCEL_GIT_COMMIT_SHA": "abc123",
        "PREVIEW_COMMIT_SHA": "abc123",
        "PR_URL": "https://github.com/owner/repo/pull/42",
        "PR_TITLE": title,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "closed"},
        {"repo": "someone/fork"},
        {"ref": "main"},
        {"ref": "staging"},
    ],
)
def test_reset_rejects_other_targets_before_export(tmp_path, reset_job, overrides):
    result, env_file = _run_reset_guard(tmp_path, reset_job, **overrides)
    assert result.returncode != 0
    assert not env_file.exists()
