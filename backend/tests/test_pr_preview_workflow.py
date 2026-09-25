import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/pr-preview.yml"
RESET_WORKFLOW = REPO / ".github/workflows/preview-reset.yml"
PRUNE_WORKFLOW = REPO / ".github/workflows/preview-prune.yml"
PREVIEW = REPO / ".github/scripts/preview"
PREPARE = PREVIEW / "prepare_preview_database.sh"
DEPLOY = PREVIEW / "deploy_preview_backend.sh"
RUN_GKE_TEARDOWN = PREVIEW / "run_gke_teardown.sh"
STOP_MODAL_APP = PREVIEW / "stop_modal_preview_app.sh"
PREVIEW_URL = "https://abundant-ai-preview--oddish-pr-0-api.modal.run"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _wf():
    return yaml.safe_load(WORKFLOW.read_text())


def _needs(job):
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


@pytest.mark.parametrize(
    ("workflow", "job", "step", "pr_number"),
    [
        (
            WORKFLOW,
            "prepare-preview-database",
            "Prepare preview database",
            "github.event.pull_request.number",
        ),
        (RESET_WORKFLOW, "reset", "Rebuild preview database", "inputs.pr_number"),
    ],
)
def test_archil_override_is_selected_for_this_preview(workflow, job, step, pr_number):
    steps = yaml.safe_load(workflow.read_text())["jobs"][job]["steps"]
    env = next(item["env"] for item in steps if item["name"] == step)
    assert env["ARCHIL_PREVIEW_CONFIG"] == (
        "${{ secrets[format('ARCHIL_PREVIEW_{0}', " + pr_number + ")] }}"
    )


@needs_bash
@pytest.mark.parametrize("override", [False, True])
def test_publish_preview_archil_credentials(tmp_path, override):
    config = (
        {
            "ARCHIL_API_KEY": "test-blue-key",
            "ARCHIL_BASE_URL": "https://blue.example.com",
            "ARCHIL_REGION": "aws-us-east-1",
        }
        if override
        else {}
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "python").symlink_to(sys.executable)
    capture = tmp_path / "capture.json"
    uv = bins / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--from-json') + 1])\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'args': sys.argv[1:], 'values': json.loads(p.read_text()), "
        "'mode': p.stat().st_mode & 0o777, 'file': str(p)}))\n"
    )
    uv.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(PREVIEW / "publish_modal_db_secret.sh")],
        env={
            **os.environ,
            "PATH": f"{bins}:{os.environ['PATH']}",
            "MODAL_ENVIRONMENT": "preview",
            "MODAL_APP_NAME": "oddish-pr-123",
            "ODDISH_RUNTIME_DATABASE_URL": "postgresql://preview-only",
            "ARCHIL_PREVIEW_CONFIG": json.dumps(config) if override else "",
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    published = json.loads(capture.read_text())
    assert published["values"] == {
        "ODDISH_DATABASE_URL": "postgresql://preview-only",
        **config,
    }
    assert published["args"][:7] == [
        "run",
        "modal",
        "secret",
        "create",
        "--env",
        "preview",
        "--force",
    ]
    assert published["args"][7] == "oddish-pr-123-db"
    assert published["mode"] == 0o600
    assert not Path(published["file"]).exists()
    for value in published["values"].values():
        assert value not in proc.stdout + proc.stderr + " ".join(published["args"])


def test_every_same_repo_pr_prepares_a_database_without_change_detection():
    job = _wf()["jobs"]["prepare-preview-database"]
    assert _needs(job) == []
    condition = job["if"]
    assert "head.repo.full_name == github.repository" in condition
    assert "event.action != 'closed'" in condition
    assert "head.ref != 'main'" in condition
    assert "head.ref != 'staging'" in condition
    assert "outputs" not in condition
    assert "find_last_deploys" not in WORKFLOW.read_text()
    assert "compute_deployment_plan" not in WORKFLOW.read_text()


@pytest.mark.parametrize(
    "name",
    [
        "prepare-preview-database",
        "deploy-preview-backend",
        "update-vercel-preview",
        "post-preview-links",
    ],
)
def test_superseded_preview_jobs_can_be_cancelled(name):
    condition = _wf()["jobs"][name]["if"]
    assert "!cancelled()" in condition
    assert "always()" not in condition


def test_disposable_preview_backend_does_not_enable_ec2():
    env = _wf()["jobs"]["deploy-preview-backend"]["env"]
    assert not any(name.startswith("ODDISH_EC2_") for name in env)


def test_post_links_waits_for_backend_and_vercel():
    job = _wf()["jobs"]["post-preview-links"]
    needs = _needs(job)
    assert "deploy-preview-backend" in needs
    assert "update-vercel-preview" in needs
    modal_url = job["env"]["MODAL_API_URL"]
    assert "needs.deploy-preview-backend.outputs.modal_api_url" in modal_url
    assert "||" not in modal_url


def test_every_preview_uses_its_own_backend_and_neon_branch():
    jobs = _wf()["jobs"]
    for name in ("deploy-preview-backend", "update-vercel-preview"):
        assert _needs(jobs[name]) == ["prepare-preview-database"]
        assert jobs[name]["if"].strip() == (
            "!cancelled() &&\nneeds.prepare-preview-database.result == 'success'"
        )
    for name in ("update-vercel-preview", "post-preview-links"):
        env = jobs[name]["env"]
        assert "branch_id" in env["NEON_BRANCH_ID"]
        assert "||" not in env["NEON_BRANCH_ID"]
        assert "STAGING_API_URL" not in env
        assert "PROD_API_URL" not in env
        assert "NEON_STAGING_BRANCH_ID" not in env
    assert jobs["update-vercel-preview"]["env"]["MODAL_API_URL"] == (
        "${{ needs.prepare-preview-database.outputs.preview_api_url }}"
    )


def _run_preview_gate(tmp_path, **overrides):
    step = next(
        step
        for step in _wf()["jobs"]["require-working-preview"]["steps"]
        if step.get("id") == "verify"
    )
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--output" ]; then\n'
        "    shift\n"
        '    [ "$1" = /dev/null ] || echo \'{"openapi":"3.1.0"}\' > "$1"\n'
        "  fi\n"
        "  shift\n"
        "done\n"
        "printf 200\n"
    )
    curl.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "SAME_REPO": "true",
            "PR_HEAD_REF": "frontend-change",
            "DEPLOY_URL": "https://pr-0.oddish.app",
            "PREPARE_RESULT": "success",
            "BACKEND_RESULT": "success",
            "VERCEL_RESULT": "success",
            "DB_BRANCH_ID": "br-preview-0",
            "PREVIEW_URL": "https://preview.vercel.app",
            "PREVIEW_ALIAS_URL": "https://pr-0.oddish.app",
            "BACKEND_API_URL": PREVIEW_URL,
            "FRONTEND_BACKEND_API_URL": PREVIEW_URL,
            "EXPECTED_BACKEND_API_URL": PREVIEW_URL,
            **overrides,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_required_preview_gate_accepts_its_own_complete_stack(tmp_path):
    result = _run_preview_gate(tmp_path)
    assert result.returncode == 0, result.stderr
    summary = (tmp_path / "summary").read_text()
    assert "Backend preview is ready" in summary
    assert "Database preview branch: `br-preview-0`" in summary


@needs_bash
@pytest.mark.parametrize(
    "missing",
    [
        {"PREPARE_RESULT": "skipped"},
        {"BACKEND_RESULT": "skipped"},
        {"VERCEL_RESULT": "skipped"},
        {"DB_BRANCH_ID": ""},
        {"BACKEND_API_URL": "https://staging.modal.run"},
        {"FRONTEND_BACKEND_API_URL": "https://staging.modal.run"},
        {"EXPECTED_BACKEND_API_URL": ""},
    ],
)
def test_required_preview_gate_rejects_a_missing_or_shared_resource(tmp_path, missing):
    result = _run_preview_gate(tmp_path, **missing)
    assert result.returncode == 1
    assert "::error::" in result.stdout


@needs_bash
@pytest.mark.parametrize("head_ref", ["main", "staging"])
@pytest.mark.parametrize("same_repo", ["true", "false"])
def test_reserved_branches_skip_resources_only_in_the_same_repo(
    tmp_path, head_ref, same_repo
):
    result = _run_preview_gate(
        tmp_path,
        PR_HEAD_REF=head_ref,
        SAME_REPO=same_repo,
        PREPARE_RESULT="skipped",
        BACKEND_RESULT="skipped",
        VERCEL_RESULT="skipped",
        DB_BRANCH_ID="",
    )
    assert result.returncode == (0 if same_repo == "true" else 1)


@pytest.mark.parametrize(
    "log",
    [
        f"Created web function api_app => {PREVIEW_URL}",
        (
            f"Created web function api_app =>\n│   {PREVIEW_URL}\n"
            f"Created web function qa_model_gateway =>\n│   {PREVIEW_URL.replace('-api.', '-api-qa-model.')}"
        ),
        f"{PREVIEW_URL.replace('-api.', '-api-qa-model.')}\n{PREVIEW_URL}",
        f"{PREVIEW_URL}\n{PREVIEW_URL}",
    ],
)
def test_extract_modal_api_url_ignores_gateway_and_duplicate_output(tmp_path, log):
    log_path = tmp_path / "deploy.log"
    log_path.write_text(log)
    result = subprocess.run(
        [sys.executable, str(PREVIEW / "extract_modal_api_url.py"), str(log_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == PREVIEW_URL


@pytest.mark.parametrize(
    "log",
    [
        "Deployment failed",
        PREVIEW_URL.replace("-api.", "-api-qa-model."),
        f"{PREVIEW_URL}\n{PREVIEW_URL.replace('pr-0-', 'pr-1-')}",
    ],
)
def test_extract_modal_api_url_rejects_missing_or_ambiguous_api(tmp_path, log):
    log_path = tmp_path / "deploy.log"
    log_path.write_text(log)
    result = subprocess.run(
        [sys.executable, str(PREVIEW / "extract_modal_api_url.py"), str(log_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Expected one Modal API URL" in result.stderr


@needs_bash
def test_reset_teardown_timeout_keeps_existing_modal_app_and_blocks_reset():
    tmp = Path(tempfile.mkdtemp())
    preview = tmp / "preview"
    preview.mkdir()
    shutil.copy(STOP_MODAL_APP, preview / STOP_MODAL_APP.name)
    helper = preview / RUN_GKE_TEARDOWN.name
    helper.write_text("#!/usr/bin/env bash\nexit 124\n")
    helper.chmod(0o755)
    modal_calls = tmp / "modal-calls"
    fake_modal = tmp / "modal"
    fake_modal.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{modal_calls}"\n'
        'if [ "$2" = list ]; then echo \'[{"description":"oddish-pr-1366","state":"deployed"}]\'; fi\n'
    )
    fake_modal.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(preview / STOP_MODAL_APP.name)],
        env={
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "MODAL_ENVIRONMENT": "preview",
            "MODAL_APP_NAME": "oddish-pr-1366",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "GKE teardown failed" in proc.stderr
    assert "leaving oddish-pr-1366 running" in proc.stderr
    assert "app stop" not in modal_calls.read_text()


def _run_prepare(
    *,
    branch_was_created="false",
    migrate_exit=0,
    want_summary=False,
    approval_exit=0,
    expect_exit=0,
):
    tmp = Path(tempfile.mkdtemp())
    pv = tmp / "preview"
    pv.mkdir()
    (tmp / "backend").mkdir()
    bins = tmp / "bin"
    bins.mkdir()
    order = tmp / "order"
    shutil.copy(PREPARE, pv / "prepare_preview_database.sh")

    def stub(name, body):
        p = pv / name
        p.write_text(f"#!/usr/bin/env bash\nset -e\n{body}\n")
        p.chmod(0o755)

    (tmp / "neon_database.py").write_text(
        "import os\nfrom pathlib import Path\n"
        f"with Path({str(order)!r}).open('a') as f: f.write('neon\\n')\n"
        "with open(os.environ['GITHUB_OUTPUT'], 'a') as f:\n"
        "    f.write('branch_ref=br123\\nbranch_id=br123\\n')\n"
        f"    f.write('branch_was_created={branch_was_created}\\n')\n"
        "with open(os.environ['GITHUB_ENV'], 'a') as f:\n"
        "    f.write('ODDISH_DATABASE_URL=postgresql://x\\n')\n"
    )
    stub("run_preview_migrations.sh", f'echo migrate >> "{order}"\nexit {migrate_exit}')
    stub("publish_modal_db_secret.sh", f'echo publish >> "{order}"')
    fake_uv = bins / "uv"
    fake_uv.write_text(
        f"#!/usr/bin/env bash\n"
        f'case "$*" in *sync_org_approvals.py*) echo approvals >> "{order}"; exit {approval_exit};; '
        "*) exit 9;; esac\n"
    )
    fake_uv.chmod(0o755)

    for name in ("summary", "out", "env"):
        (tmp / name).write_text("")
    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(tmp / "summary"),
        "GITHUB_WORKSPACE": str(tmp),
        "GITHUB_OUTPUT": str(tmp / "out"),
        "GITHUB_ENV": str(tmp / "env"),
        "MODAL_ENVIRONMENT": "preview",
        "MODAL_APP_NAME": "oddish-pr-0",
    }
    proc = subprocess.run(
        ["bash", str(pv / "prepare_preview_database.sh")],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == (expect_exit or migrate_exit), proc.stderr
    order_list = order.read_text().split()
    if want_summary:
        return order_list, (tmp / "summary").read_text()
    return order_list


def _run_deploy(emitted_url, expected_url=None):
    tmp = Path(tempfile.mkdtemp())
    pv = tmp / "preview"
    pv.mkdir()
    shutil.copy(DEPLOY, pv / "deploy_preview_backend.sh")
    dmp = pv / "deploy_modal_preview.sh"
    dmp.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        f'echo "modal_api_url={emitted_url}" >> "$GITHUB_OUTPUT"\n'
    )
    dmp.chmod(0o755)
    (pv / "wait_for_modal_ready.py").write_text("import sys\nsys.exit(0)\n")
    for name in ("summary", "out"):
        (tmp / name).write_text("")
    env = {
        **os.environ,
        "GITHUB_STEP_SUMMARY": str(tmp / "summary"),
        "GITHUB_OUTPUT": str(tmp / "out"),
        "MODAL_ENVIRONMENT": "preview",
        "MODAL_APP_NAME": "oddish-pr-0",
    }
    if expected_url is not None:
        env["EXPECTED_MODAL_API_URL"] = expected_url
    return subprocess.run(
        ["bash", str(pv / "deploy_preview_backend.sh")],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_deploy_guard_passes_on_match():
    assert _run_deploy(PREVIEW_URL, PREVIEW_URL).returncode == 0


@needs_bash
def test_deploy_guard_passes_on_trailing_slash_only():
    assert _run_deploy(PREVIEW_URL + "/", PREVIEW_URL).returncode == 0


@needs_bash
def test_deploy_guard_fails_on_mismatch():
    proc = _run_deploy("https://wrong.modal.run", PREVIEW_URL)
    assert proc.returncode == 1
    assert "mismatch" in proc.stderr


@needs_bash
def test_deploy_guard_skipped_when_expected_unset():
    assert _run_deploy("https://anything.modal.run").returncode == 0


def _reset_wf():
    return yaml.safe_load(RESET_WORKFLOW.read_text())


def _on(wf):
    # pyyaml parses a bare `on:` key as boolean True.
    return wf.get("on", wf.get(True))


def test_reset_shares_preview_concurrency_group():
    reset = _reset_wf()
    preview = _wf()
    assert str(reset["concurrency"]["group"]).startswith("pr-preview-")
    assert str(preview["concurrency"]["group"]).startswith("pr-preview-")
    assert reset["concurrency"]["cancel-in-progress"] is False


def test_reset_guard_runs_before_checkout():
    # The reset runs PR code with secrets: the same-repo/open guard must reject
    # fork PRs BEFORE any PR code is checked out.
    steps = _reset_wf()["jobs"]["reset"]["steps"]
    guard_idx = next(
        i for i, s in enumerate(steps) if ".head.repo.full_name" in s.get("run", "")
    )
    checkout_idx = next(
        i for i, s in enumerate(steps) if "actions/checkout" in s.get("uses", "")
    )
    assert guard_idx < checkout_idx
    # Container jobs default to sh (dash); the guard's `set -euo pipefail`
    # needs bash (its absence failed the first live dispatch closed).
    assert steps[guard_idx].get("shell") == "bash"
    ref = steps[checkout_idx]["with"]["ref"]
    assert "refs/pull/" in ref and "/merge" in ref


def _prune_wf():
    return yaml.safe_load(PRUNE_WORKFLOW.read_text())


def test_prune_runs_on_a_schedule():
    on = _on(_prune_wf())
    assert on["schedule"], "prune must run unattended, not only on dispatch"
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["max_age_days"]["default"] == "7"


def test_prune_workflow_invokes_the_script():
    job = _prune_wf()["jobs"]["prune"]
    steps = job["steps"]
    assert any("prune_stale_preview_apps.sh" in s.get("run", "") for s in steps)
    assert not any("supabase" in s.get("run", "") for s in steps)
    assert "SUPABASE_ACCESS_TOKEN" not in job["env"]
    for key in ("NEON_API_KEY", "NEON_PROJECT_ID", "MAX_AGE_DAYS"):
        assert key in job["env"]
    # Dispatch inputs reach the script through env, never interpolated into a
    # run: body where they would be shell injection.
    assert not any("${{" in s.get("run", "") for s in steps)


def test_reset_redeploys_the_same_preview_it_replaces():
    deploy_env = _wf()["jobs"]["deploy-preview-backend"].get("env") or {}
    reset_env = (
        yaml.safe_load(RESET_WORKFLOW.read_text())["jobs"]["reset"].get("env") or {}
    )
    deploy_oddish = {k: v for k, v in deploy_env.items() if k.startswith("ODDISH_")}
    reset_oddish = {k: v for k, v in reset_env.items() if k.startswith("ODDISH_")}
    assert deploy_oddish == reset_oddish


@needs_bash
def test_failed_approval_sync_fails_prepare_after_publishing_credentials():
    order = _run_prepare(
        approval_exit=7,
        expect_exit=7,
    )
    assert order[0] == "neon"
    assert set(order[-2:]) == {"approvals", "publish"}


def test_preview_uses_neon_with_approval_only_source():
    env = _wf()["jobs"]["prepare-preview-database"]["env"]
    assert "NEON_API_KEY" in env
    assert "NEON_PROJECT_ID" in env
    assert "PREVIEW_SAMPLE_SOURCE_DB_URL" in env
    assert "SUPABASE_ACCESS_TOKEN" not in env


@needs_bash
def test_deploy_reuses_branch_without_stop_or_seed():
    order, summary = _run_prepare(want_summary=True)
    assert order[:2] == ["neon", "migrate"]
    assert set(order[2:]) == {"approvals", "publish"}
    assert "Existing preview data is kept" in summary


@needs_bash
def test_new_branch_is_checked_and_published():
    order = _run_prepare(
        branch_was_created="true",
    )
    assert order[:2] == ["neon", "migrate"]
    assert set(order[2:]) == {"approvals", "publish"}


@needs_bash
def test_migration_failure_stops_without_reset_or_publish():
    order, summary = _run_prepare(
        migrate_exit=1,
        want_summary=True,
    )
    assert order == ["neon", "migrate"]
    assert "Schema upgraded to head: `false`" in summary


def test_preview_cleanup_uses_neon_and_skips_main_and_staging():
    condition = _wf()["jobs"]["stop-preview"]["if"]
    assert "head.ref != 'main'" in condition
    assert "head.ref != 'staging'" in condition
    cleanup = (PREVIEW / "stop_preview.sh").read_text()
    assert 'neon_database.py" delete-preview' in cleanup
    assert "supabase branches" not in cleanup
