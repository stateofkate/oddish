"""The ephemeral parent must settle from the same facts the in-process path has.

Harbor writes its job-level ``result.json`` without ``trial_results``. The
ephemeral parent rebuilds ``JobResult`` from that file, so the per-trial
exception and phase timing are absent unless Oddish reads them back off disk.

The fixtures below build real Harbor models and serialize them exactly as
Harbor does, so a change to Harbor's schema breaks these tests instead of
quietly producing a fixture that Harbor would never write.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from harbor.models.job.result import AgentDatasetStats, JobResult
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import TaskConfig, TrialConfig
from harbor.models.trial.result import (
    AgentInfo,
    ExceptionInfo,
    TimingInfo,
    TrialResult,
)
from harbor.models.verifier.result import VerifierResult
from oddish.db import TrialStatus
from oddish.workers.harbor.ephemeral import _read_outcome
from oddish.workers.harbor.outcome import _extract_outcome_from_job_result
from oddish.workers.queue.trial_handler import _store_trial_results
from test_scoreless_trial_no_retry import _patch_session, _trial

TRIAL_NAME = "my-task__claude-code__1"


def _trial_result(
    *,
    exception_type: str | None,
    exception_message: str | None = None,
    http_status: int | None = None,
    reward: float | None = None,
) -> TrialResult:
    started = datetime(2026, 9, 14, 14, 4, 0, tzinfo=timezone.utc)
    exception_info = None
    if exception_type is not None:
        exception_info = ExceptionInfo(
            exception_type=exception_type,
            exception_message=(
                exception_message
                or f"{exception_type}: the provider rejected the request"
            ),
            exception_traceback="",
            occurred_at=started,
            http_status=http_status,
        )
    verifier_result = None
    if reward is not None:
        verifier_result = VerifierResult(rewards={"reward": reward})
    return TrialResult(
        task_name="my-task",
        trial_name=TRIAL_NAME,
        trial_uri=f"file:///jobs/job-1/{TRIAL_NAME}",
        task_id=LocalTaskId(path=Path("/tasks/my-task")),
        task_checksum="deadbeef",
        config=TrialConfig(task=TaskConfig(path=Path("/tasks/my-task"))),
        agent_info=AgentInfo(name="claude-code", version="1.0.0"),
        verifier_result=verifier_result,
        exception_info=exception_info,
        agent_execution=TimingInfo(
            started_at=started, finished_at=started + timedelta(seconds=7)
        ),
        verifier=TimingInfo(
            started_at=started + timedelta(seconds=7),
            finished_at=started + timedelta(seconds=11),
        ),
    )


def _ephemeral_job_dir(
    tmp_path: Path, trial_result: TrialResult, *, stats_reward: float | None = None
) -> tuple[Path, Path]:
    """Write a job dir the way Harbor writes one, then return its paths.

    Harbor calls ``_write_job_result(exclude_trial_results=True)`` for the job
    summary and writes each trial's full result in the trial's own directory.
    """
    job_dir = tmp_path / "jobs" / "job-1"
    trial_dir = job_dir / TRIAL_NAME
    trial_dir.mkdir(parents=True)

    stats: dict[str, object] = {}
    if stats_reward is not None:
        stats = {
            "evals": {
                "claude-code__my-task": AgentDatasetStats(
                    reward_stats={"reward": {stats_reward: [str(trial_result.id)]}}
                )
            }
        }
    job_result = JobResult(
        id=trial_result.id,
        started_at=datetime(2026, 9, 14, 14, 4, 0, tzinfo=timezone.utc),
        n_total_trials=1,
        stats=stats,
        trial_results=[trial_result],
    )
    job_result_path = job_dir / "result.json"
    job_result_path.write_text(
        job_result.model_dump_json(exclude={"trial_results"}), encoding="utf-8"
    )
    (trial_dir / "result.json").write_text(
        trial_result.model_dump_json(), encoding="utf-8"
    )
    return job_dir, job_result_path


def _read_ephemeral_outcome(
    tmp_path: Path,
    trial_result: TrialResult,
    *,
    stats_reward: float | None = None,
    environment_provider: str | None = None,
):
    job_dir, job_result_path = _ephemeral_job_dir(
        tmp_path, trial_result, stats_reward=stats_reward
    )
    outcome_path = tmp_path / "outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "job_dir": str(job_dir),
                "job_result_path": str(job_result_path),
                "duration_sec": 11.0,
                "error": None,
                "exception_type": None,
            }
        ),
        encoding="utf-8",
    )
    return _read_outcome(
        outcome_path=outcome_path,
        unique_parent=job_dir,
        returncode=0,
        duration=11.0,
        stderr="",
        stdout_tail="",
        environment_provider=environment_provider,
    )


def test_the_job_summary_harbor_writes_has_no_trial_results(tmp_path):
    # Guards the premise of this change. Harbor excludes ``trial_results`` from
    # the job summary, so the parent cannot read the exception from that file.
    _, job_result_path = _ephemeral_job_dir(
        tmp_path, _trial_result(exception_type="ApiClientError", http_status=404)
    )

    assert (
        JobResult.model_validate_json(job_result_path.read_text()).trial_results == []
    )


def test_ephemeral_outcome_recovers_the_provider_exception(tmp_path):
    outcome = _read_ephemeral_outcome(
        tmp_path,
        _trial_result(exception_type="ApiClientError", http_status=404, reward=0.0),
    )

    assert outcome.exception_type == "ApiClientError"
    assert outcome.http_status == 404
    assert outcome.error == "ApiClientError: the provider rejected the request"


@pytest.mark.parametrize("provider", ["thunder", "modal"])
def test_disk_recovered_capacity_error_keeps_provider_context(tmp_path, provider):
    outcome = _read_ephemeral_outcome(
        tmp_path,
        _trial_result(exception_type="CapacityError", http_status=503),
        environment_provider=provider,
    )

    assert outcome.exception_type == "CapacityError"
    assert outcome.http_status == 503
    assert outcome.provider_error_code == (
        "sandbox_capacity_unavailable" if provider == "thunder" else None
    )


def test_ephemeral_outcome_recovers_phase_timing(tmp_path):
    outcome = _read_ephemeral_outcome(
        tmp_path, _trial_result(exception_type=None, reward=1.0)
    )

    assert outcome.phase_timing is not None
    assert outcome.phase_timing["agent_execution"]["duration_sec"] == 7.0
    assert outcome.phase_timing["verifier"]["duration_sec"] == 4.0


def test_ephemeral_outcome_keeps_a_clean_run_unchanged(tmp_path):
    outcome = _read_ephemeral_outcome(
        tmp_path, _trial_result(exception_type=None, reward=1.0)
    )

    assert outcome.exception_type is None
    assert outcome.error is None
    assert outcome.reward == 1.0


@pytest.mark.asyncio
async def test_ephemeral_provider_rejection_settles_without_a_score(
    monkeypatch, tmp_path
):
    # The end of the chain this change exists for. The incident's trials took
    # this path, so the reward rule could not read an exception here.
    outcome = _read_ephemeral_outcome(
        tmp_path,
        _trial_result(exception_type="ApiClientError", http_status=404, reward=0.0),
    )
    trial = _trial(
        agent="claude-code",
        model="anthropic/claude-opus-4-7",
        attempts=1,
        max_attempts=3,
    )
    _patch_session(monkeypatch, trial)

    await _store_trial_results(
        trial_id=trial.id,
        outcome=outcome,
        trial_s3_key=None,
        execution_error=None,
        trial_attempt=trial.attempts,
    )

    assert trial.reward is None
    assert trial.status == TrialStatus.FAILED
    assert trial.error_message == "ApiClientError: the provider rejected the request"
    assert trial.result["harbor_exception"]["http_status"] == 404


def test_ephemeral_outcome_prefers_the_job_stats_reward(tmp_path):
    # Harbor keeps job-level ``stats`` in the summary it writes, so the reward
    # already survived the strip. That resolution order must not change: the
    # stats reward still wins over the reward on a recovered trial result.
    outcome = _read_ephemeral_outcome(
        tmp_path,
        _trial_result(exception_type=None, reward=1.0),
        stats_reward=0.5,
    )

    assert outcome.reward == 0.5


def _write_terminal_boundary(
    job_dir: Path, reward: float, *, abort: bool = False
) -> None:
    verifier = job_dir / TRIAL_NAME / "verifier"
    rundir = verifier / "rundir"
    rundir.mkdir(parents=True)
    (rundir / "agent-boundary.json").write_text(
        json.dumps(
            {
                "success": True,
                "reason": "declared",
                "exempt_log_pipeline_pids": [952, 955],
                "sigkill_pids": [952, 3842],
            }
        ),
        encoding="utf-8",
    )
    (rundir / "episode_done.json").write_text(
        json.dumps(
            {
                "done": True,
                "freeze_reason": "declared",
                "completion_reason": "declared_soak_complete",
            }
        ),
        encoding="utf-8",
    )
    (rundir / "verdict.json").write_text(
        json.dumps({"overall": "PASS" if reward == 1 else "FAIL"}),
        encoding="utf-8",
    )
    (verifier / "reward.json").write_text(
        json.dumps({"reward": reward}), encoding="utf-8"
    )
    if abort:
        (verifier / "verifier_abort.json").write_text(
            json.dumps({"aborted": True}), encoding="utf-8"
        )


@pytest.mark.parametrize("reward", [0.0, 1.0])
@pytest.mark.parametrize(
    "agent,model,exception_type",
    [
        ("grok-build", "xai/grok-4.7", "NetworkConnectionError"),
        ("claude-code", "anthropic/claude-opus-4-7", "AgentExecutionError"),
    ],
)
@pytest.mark.asyncio
async def test_declared_freezer_kill_retains_completed_verifier_score(
    monkeypatch, tmp_path, reward, agent, model, exception_type
):
    result = _trial_result(
        exception_type=exception_type,
        exception_message=f"Command failed (exit 137): {agent} | tee",
        reward=reward,
    )
    job_dir, job_result_path = _ephemeral_job_dir(tmp_path, result)
    _write_terminal_boundary(job_dir, reward)
    outcome = _extract_outcome_from_job_result(
        JobResult.model_validate_json(job_result_path.read_text()),
        job_result_path,
        job_dir,
        11.0,
    )
    assert outcome.expected_boundary_kill is True
    trial = _trial(agent=agent, model=model, attempts=1, max_attempts=3)
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id=trial.id,
        outcome=outcome,
        trial_s3_key=None,
        execution_error=None,
        trial_attempt=trial.attempts,
    )
    assert trial.status == TrialStatus.SUCCESS
    assert trial.reward == reward
    assert trial.error_message is None
    assert trial.result["expected_boundary_kill"] is True
    assert trial.result["harbor_exception"]["exception_type"] == exception_type


@pytest.mark.parametrize(
    "invalid_evidence", ["missing", "abort", "wrong_pid", "wrong_reward", "wrong_error"]
)
def test_freezer_kill_without_complete_receipt_is_still_an_error(
    tmp_path, invalid_evidence
):
    error = (
        "Command failed (exit 137): grok | tee"
        if invalid_evidence != "wrong_error"
        else "API request timed out"
    )
    result = _trial_result(
        exception_type="NetworkConnectionError",
        exception_message=error,
        reward=0.0,
    )
    job_dir, job_result_path = _ephemeral_job_dir(tmp_path, result)
    if invalid_evidence != "missing":
        _write_terminal_boundary(job_dir, 0.0, abort=invalid_evidence == "abort")
    verifier = job_dir / TRIAL_NAME / "verifier"
    if invalid_evidence == "wrong_pid":
        boundary_path = verifier / "rundir/agent-boundary.json"
        boundary = json.loads(boundary_path.read_text())
        boundary["sigkill_pids"] = [3842]
        boundary_path.write_text(json.dumps(boundary))
    if invalid_evidence == "wrong_reward":
        (verifier / "reward.json").write_text(json.dumps({"reward": 1.0}))
    outcome = _extract_outcome_from_job_result(
        JobResult.model_validate_json(job_result_path.read_text()),
        job_result_path,
        job_dir,
        11.0,
    )
    assert outcome.expected_boundary_kill is False
    assert outcome.error == error
