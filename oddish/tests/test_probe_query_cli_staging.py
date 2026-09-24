from oddish.worker.probe_staging import (
    stage_query_cli,
    stage_cli_mount,
)


def test_stage_query_cli_copies_executable(tmp_path):
    stage_query_cli(tmp_path)
    cli = tmp_path / "oddish-query"
    assert cli.exists() and cli.read_text().startswith("#!/usr/bin/env node")


def test_stage_cli_mount_writes_only_the_cli(tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    stage_cli_mount(harness)
    entries = sorted(p.name for p in harness.iterdir())
    assert entries == ["oddish-query"]
    cli = harness / "oddish-query"
    assert cli.read_text().startswith("#!/usr/bin/env node")
    # executable bit set
    assert cli.stat().st_mode & 0o111


def test_stage_cli_mount_adds_only_the_qa_submission_contract(tmp_path):
    from oddish.worker.probe_staging import apply_analysis_overlay

    task = tmp_path / "task"
    task.mkdir()
    apply_analysis_overlay(
        task,
        brief="qa brief",
        artifact="qa_result.json",
        check_payload={"kind": "qa", "trial_ids": []},
    )
    harness = tmp_path / "harness"
    stage_cli_mount(harness, analysis_task_dir=task)

    assert sorted(path.name for path in harness.iterdir()) == [
        ".analysis-contract",
        "oddish-query",
        "submit-analysis-result",
    ]
    assert (harness / "submit-analysis-result").stat().st_mode & 0o111
