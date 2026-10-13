"""Adversarial trials: launch rules, QA ordering, review, findings and preparation.

Each test checks one rule; the rule is in the test name. Database tests use
real Postgres rows and replace only artifact transport, as the audit tests do.
"""

import asyncio
import copy
import hashlib
from importlib import resources
import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text

from oddish.core.adversarial import (
    ADVERSARIAL_TRIAL_KIND,
    ATTACK_PROMPT_VERSION,
    REPLAY_AGENT,
    attack_prompt,
    replay_findings,
)
from oddish.schemas import TrialSpec
from oddish.core.endpoints.qa import launch_adversarial_trial_core
from oddish.core.trial_control import cancel_trials_by_id
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    init_db,
    utcnow,
)
from oddish.queue import (
    maybe_start_task_qa_stage,
)
from oddish.workers import analysis_trials

CONTENT_HASH = "hash-of-version-one"


@pytest_asyncio.fixture
async def attack_task(monkeypatch, tmp_path):
    """A finished task: solver, oracle (passes), nop (fails), a published verdict."""
    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("ODDISH_DATABASE_URL not set")
    await init_db()
    task_id = "adversarial-" + uuid.uuid4().hex[:12]
    version_id = task_id + "-v1"
    async with get_session() as session:
        experiment = ExperimentModel(name=task_id)
        task = TaskModel(
            id=task_id,
            name=task_id,
            user="test",
            task_path="p",
            status=TaskStatus.COMPLETED,
            run_analysis=True,
            verdict={"is_good": True},
            verdict_status=VerdictStatus.SUCCESS,
        )
        session.add_all([experiment, task])
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="p",
                content_hash=CONTENT_HASH,
                pre_trial_status=VerdictStatus.SUCCESS,
                pre_trial_started_at=utcnow(),
                pre_trial_finished_at=utcnow(),
                pre_trial={"items": []},
            )
        )
        await session.flush()
        task.current_version_id = version_id
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:task, :experiment, NOW())"
            ),
            {"task": task_id, "experiment": experiment.id},
        )
        for suffix, agent, reward in (
            ("solver", "claude-code", 0.0),
            ("oracle", "oracle", 1.0),
            ("nop", "nop", 0.0),
        ):
            session.add(
                TrialModel(
                    id=f"{task_id}-{suffix}",
                    name=suffix,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment.id,
                    agent=agent,
                    provider="local",
                    queue_key="test",
                    kind="agent",
                    status=TrialStatus.SUCCESS,
                    reward=reward,
                    has_trajectory=agent == "claude-code",
                    finished_at=utcnow() - timedelta(hours=1),
                )
            )
    task_dir = tmp_path / "task"
    _write_task(task_dir)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        version.task_path = str(task_dir)
    artifacts: dict = {}

    async def read_artifact(trial, filename):
        return copy.deepcopy(artifacts.get(trial.id))

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)
    monkeypatch.setattr(
        analysis_trials, "read_own_trajectory", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(analysis_trials, "_fire_qa_imported", AsyncMock())
    # Launch rules are checked one attack at a time; the batch size has its own test.
    monkeypatch.setattr("oddish.core.endpoints.qa.ATTACKS_PER_LAUNCH", 1)
    monkeypatch.setattr(
        "oddish.core.trial_io.read_trial_trajectory", AsyncMock(return_value=None)
    )
    yield task_id, version_id, artifacts


async def launch(task_id, version_id, request_id="req-1", **overrides):
    async with get_session() as session:
        return await launch_adversarial_trial_core(
            session,
            task_id=task_id,
            expected_version_id=overrides.pop("expected_version_id", version_id),
            request_id=request_id,
            execution=overrides.pop("execution", TrialSpec(agent="codex", model="openai/gpt-6-luna")),
            **overrides,
        )


async def attack_trials(task_id):
    async with get_session() as session:
        return list(
            await session.scalars(
                select(TrialModel).where(
                    TrialModel.task_id == task_id,
                    TrialModel.kind == ADVERSARIAL_TRIAL_KIND,
                    TrialModel.agent != REPLAY_AGENT,
                )
            )
        )


async def replay_trials(task_id):
    async with get_session() as session:
        return list(
            await session.scalars(
                select(TrialModel).where(
                    TrialModel.task_id == task_id,
                    TrialModel.agent == REPLAY_AGENT,
                )
            )
        )


async def qa_trials(task_id):
    async with get_session() as session:
        return list(
            await session.scalars(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "qa"
                )
            )
        )


async def settle_attack(
    trial_id, *, reward=1.0, status=TrialStatus.SUCCESS, confirmed=True
):
    """Finish an attack; ``confirmed`` also records a replay that scored 1."""
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        trial.status = status
        trial.reward = reward
        trial.attempts = 1
        trial.has_trajectory = True
        trial.finished_at = utcnow()
        if confirmed:
            session.add(
                TrialModel(
                    id=f"{trial_id}-replay",
                    name="replay",
                    task_id=trial.task_id,
                    task_version_id=trial.task_version_id,
                    experiment_id=trial.experiment_id,
                    agent=REPLAY_AGENT,
                    provider="local",
                    queue_key="test",
                    kind=ADVERSARIAL_TRIAL_KIND,
                    status=TrialStatus.SUCCESS,
                    reward=1.0,
                    attempts=1,
                    finished_at=utcnow(),
                    harbor_config={
                        ADVERSARIAL_TRIAL_KIND: {
                            "replay_of": trial_id,
                            "attack_attempt": 1,
                        }
                    },
                )
            )


async def admit(task_id):
    async with get_session() as session:
        return await maybe_start_task_qa_stage(session, task_id)


# --- 1 & 2: request rules and the launch transaction -----------------------


@pytest.mark.asyncio
async def test_launch_creates_one_queued_trial_with_its_worker_job_and_pins(
    attack_task,
):
    task_id, version_id, _ = attack_task
    result = await launch(task_id, version_id)
    assert result["status"] == "queued"
    [trial] = await attack_trials(task_id)
    assert result["trial_id"] == trial.id
    assert trial.status == TrialStatus.QUEUED
    assert trial.task_version_id == version_id
    pins = trial.harbor_config[ADVERSARIAL_TRIAL_KIND]
    assert pins["request_id"] == "req-1"
    assert pins["task_content_hash"] == CONTENT_HASH
    assert pins["prompt_version"] == ATTACK_PROMPT_VERSION
    assert pins["execution"]["agent"] == "codex"
    assert trial.timeout_minutes is None
    assert trial.max_attempts == 1
    assert pins["execution"]["model"] == trial.model
    assert pins["execution"]["environment"] == trial.environment
    async with get_session() as session:
        jobs = (
            await session.execute(
                text(
                    "SELECT status::text FROM worker_jobs WHERE kind::text = 'TRIAL' "
                    "AND subject_table = 'trials' AND subject_id = :id"
                ),
                {"id": trial.id},
            )
        ).all()
        task = await session.get(TaskModel, task_id)
    assert [row[0] for row in jobs] == ["QUEUED"]
    # The published verdict stays until a replay confirms an exploit.
    assert task.verdict == {"is_good": True}
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeating_a_request_returns_the_same_trial(attack_task):
    task_id, version_id, _ = attack_task
    first = await launch(task_id, version_id)
    again = await launch(task_id, version_id)
    assert again["status"] == "existing"
    assert again["trial_id"] == first["trial_id"]
    assert len(await attack_trials(task_id)) == 1


@pytest.mark.asyncio
async def test_a_different_request_during_an_active_run_returns_that_run(attack_task):
    task_id, version_id, _ = attack_task
    first = await launch(task_id, version_id)
    other = await launch(task_id, version_id, request_id="req-2")
    assert other["status"] == "already_running"
    assert other["trial_id"] == first["trial_id"]
    assert len(await attack_trials(task_id)) == 1


@pytest.mark.asyncio
async def test_an_explicit_rerun_after_the_run_finished_creates_a_new_trial(
    attack_task,
):
    task_id, version_id, _ = attack_task
    first = await launch(task_id, version_id)
    await settle_attack(first["trial_id"])
    rerun = await launch(task_id, version_id, request_id="req-2")
    assert rerun["status"] == "queued"
    assert rerun["trial_id"] != first["trial_id"]
    assert len(await attack_trials(task_id)) == 2


@pytest.mark.asyncio
async def test_a_live_replay_blocks_a_rerun_and_a_deleted_attack_does_not(attack_task):
    from oddish.core.adversarial import queue_exploit_replay

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(attack_id, confirmed=False)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        attack = await session.get(TrialModel, attack_id)
        replay = await queue_exploit_replay(
            session, task=task, attack=attack, script="#!/bin/sh\ntrue\n"
        )
    other = await launch(task_id, version_id, request_id="req-2")
    assert other["status"] == "already_running"
    # The replay's attack batch, so the client sees the launch it is waiting on.
    assert other["trial_ids"] == [attack_id]
    async with get_session() as session:
        (await session.get(TrialModel, attack_id)).deleted_at = utcnow()
    # With its attack gone, the live replay alone still blocks a new batch.
    orphan = await launch(task_id, version_id, request_id="req-3")
    assert orphan["status"] == "already_running"
    assert orphan["trial_ids"] == [replay.id]
    async with get_session() as session:
        (await session.get(TrialModel, replay.id)).status = TrialStatus.SUCCESS
    again = await launch(task_id, version_id)
    assert again["status"] == "queued"
    assert again["trial_id"] != attack_id


@pytest.mark.asyncio
async def test_two_simultaneous_requests_create_one_run(attack_task):
    task_id, version_id, _ = attack_task
    results = await asyncio.gather(
        launch(task_id, version_id, request_id="req-a"),
        launch(task_id, version_id, request_id="req-b"),
        launch(task_id, version_id, request_id="req-a"),
    )
    assert len({r["trial_id"] for r in results}) == 1
    assert sorted(r["status"] for r in results).count("queued") == 1
    assert len(await attack_trials(task_id)) == 1


@pytest.mark.asyncio
async def test_a_version_mismatch_changes_nothing(attack_task):
    task_id, version_id, _ = attack_task
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id, expected_version_id="some-other-version")
    assert error.value.status_code == 409
    assert await attack_trials(task_id) == []
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict == {"is_good": True}
    assert task.verdict_status == VerdictStatus.SUCCESS


@pytest.mark.asyncio
async def test_another_organization_cannot_launch(attack_task):
    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (await session.get(TaskModel, task_id)).org_id = None
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id, org_id="someone-else")
    assert error.value.status_code == 404
    assert await attack_trials(task_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_state", ["missing", "queued", "running", "failed", "skipped", "wrong_reward"])
async def test_manual_attack_queues_without_waiting_for_baselines(attack_task, baseline_state):
    task_id, version_id, _ = attack_task
    async with get_session() as session:
        for agent in ("oracle", "nop"):
            baseline = await session.get(TrialModel, f"{task_id}-{agent}")
            if baseline_state == "missing":
                await session.delete(baseline)
            elif baseline_state == "wrong_reward":
                baseline.reward = 0.0 if agent == "oracle" else 1.0
            else:
                baseline.status = TrialStatus(baseline_state)
                baseline.reward = None
    result = await launch(task_id, version_id)
    assert result["status"] == "queued"
    async with get_session() as session:
        attack = await session.get(TrialModel, result["trial_id"])
        assert attack.status == TrialStatus.QUEUED
        assert attack.task_version_id == version_id
        state = await session.scalar(text(
            "SELECT status::text FROM worker_jobs WHERE subject_table = 'trials' AND subject_id = :id"
        ), {"id": attack.id})
        assert state == "QUEUED"


# --- 4: completion order, recovery and cancellation ------------------------


async def make_solver_active(task_id):
    async with get_session() as session:
        solver = await session.get(TrialModel, f"{task_id}-solver")
        solver.status = TrialStatus.RUNNING
        solver.reward = None
        (await session.get(TaskModel, task_id)).status = TaskStatus.RUNNING


async def finish_solver(task_id):
    async with get_session() as session:
        solver = await session.get(TrialModel, f"{task_id}-solver")
        solver.status = TrialStatus.SUCCESS
        solver.reward = 0.0
        solver.finished_at = utcnow()


def qa_payload(qa):
    return qa.harbor_config["analysis_payload"]


@pytest.mark.asyncio
async def test_qa_neither_waits_for_nor_reads_attacks(attack_task):
    task_id, version_id, _ = attack_task
    await make_solver_active(task_id)
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await finish_solver(task_id)
    # The attack is still queued; QA starts on the solvers alone.
    assert (await admit(task_id)).advanced is True
    [qa] = await qa_trials(task_id)
    assert qa_payload(qa)["trial_ids"] == [f"{task_id}-solver"]
    assert attack_id not in str(qa_payload(qa))


@pytest.mark.asyncio
async def test_a_cancelled_attack_leaves_the_verdict_and_starts_no_qa(attack_task):
    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    async with get_session() as session:
        trial = await session.get(TrialModel, attack_id)
        task = await session.get(TaskModel, task_id)
        task.org_id = trial.org_id = task.org_id or "org"
        org_id = task.org_id
    async with get_session() as session:
        result = await cancel_trials_by_id(
            session, trial_ids=[attack_id], org_id=org_id
        )
    assert result["cancelled_trial_ids"] == [attack_id]
    assert await qa_trials(task_id) == []
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
    assert task.verdict == {"is_good": True}
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancelling_task_qa_also_cancels_the_live_attack_and_its_job(attack_task):
    from oddish.core.endpoints.qa import cancel_task_qa_core

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    async with get_session() as session:
        result = await cancel_task_qa_core(session, task_id=task_id)
    assert result["qa_jobs_cancelled"] == 1
    async with get_session() as session:
        attack = await session.get(TrialModel, attack_id)
        job_status = await session.scalar(
            text(
                "SELECT status::text FROM worker_jobs WHERE kind::text = 'TRIAL' "
                "AND subject_id = :id"
            ),
            {"id": attack_id},
        )
    assert attack.status == TrialStatus.FAILED
    assert attack.harbor_stage == "cancelled"
    assert job_status == "CANCELLED"


@pytest.mark.asyncio
async def test_cancelling_only_attacks_leaves_a_running_task_running(attack_task):
    from oddish.core.endpoints.qa import cancel_task_qa_core

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        task.status = TaskStatus.RUNNING
        task.verdict = None
        task.verdict_status = None
    attack_id = (await launch(task_id, version_id))["trial_id"]
    async with get_session() as session:
        await cancel_task_qa_core(session, task_id=task_id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        attack = await session.get(TrialModel, attack_id)
    assert attack.harbor_stage == "cancelled"
    assert task.status == TaskStatus.RUNNING


# --- 5 & 6: review, findings, freshness -------------------------------------


# --- 3: preparing the real task environment ---------------------------------


def _write_task(root):
    (root / "environment").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "solution").mkdir()
    (root / "instruction.md").write_text("Write a function that adds numbers.\n")
    (root / "task.toml").write_text('version = "1.0"\n[agent]\ntimeout_sec = 60\n')
    (root / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (root / "tests" / "test.sh").write_text(
        "#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n"
    )
    (root / "solution" / "solve.sh").write_text("#!/bin/sh\ntrue\n")


def _snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.asyncio
async def test_preparation_changes_only_a_copy_of_the_instruction(tmp_path):
    from oddish.db.storage import task_directory_sha256
    from oddish.workers.queue.trial_handler import _prepare_trial_task
    from oddish.workers.queue.trial_steps import PreparedTrialRun

    canonical = tmp_path / "canonical" / "the-task"
    _write_task(canonical)
    before = _snapshot(canonical)
    pinned = task_directory_sha256(canonical)
    prepared_trial = PreparedTrialRun(
        task_path=str(canonical),
        task_s3_key=None,
        task_id="t",
        trial_agent="claude-code",
        trial_model="m",
        trial_environment=None,
        trial_harbor_config={
            "task_artifact": {"sha256": pinned},
            ADVERSARIAL_TRIAL_KIND: {
                "task_content_hash": "old-parser-hash",
                "prompt_text": resources.files("oddish.analyze")
                .joinpath("adversarial_prompt.txt")
                .read_text(),
                "prompt_sha256": hashlib.sha256(
                    resources.files("oddish.analyze")
                    .joinpath("adversarial_prompt.txt")
                    .read_bytes()
                ).hexdigest(),
            },
        },
        trial_kind=ADVERSARIAL_TRIAL_KIND,
    )
    prepared = await _prepare_trial_task(
        trial_id="t-adv", prepared_trial=prepared_trial
    )
    try:
        assert prepared.task_path != canonical
        assert _snapshot(canonical) == before
        copy_files = _snapshot(prepared.task_path)
        instruction = copy_files.pop("instruction.md").decode()
        assert instruction.startswith("ADVERSARIAL REVIEW OF THIS TASK'S GRADER")
        assert instruction.endswith("Write a function that adds numbers.\n")
        original = dict(before)
        original.pop("instruction.md")
        # Environment, grader, solution and task.toml are byte-identical.
        assert copy_files == original
        assert prepared.probe_agent_env is None and prepared.probe_key_id is None
    finally:
        import shutil

        shutil.rmtree(prepared.temp_task_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_preparation_refuses_contents_that_are_not_the_requested_version(
    tmp_path,
):
    from oddish.workers.queue.trial_handler import _prepare_trial_task
    from oddish.workers.queue.trial_steps import PreparedTrialRun

    canonical = tmp_path / "canonical" / "the-task"
    _write_task(canonical)
    prepared_trial = PreparedTrialRun(
        task_path=str(canonical),
        task_s3_key=None,
        task_id="t",
        trial_agent="claude-code",
        trial_model="m",
        trial_environment=None,
        trial_harbor_config={"task_artifact": {"sha256": "0" * 64}},
        trial_kind=ADVERSARIAL_TRIAL_KIND,
    )
    before = _snapshot(canonical)
    with pytest.raises(ValueError, match="contents changed"):
        await _prepare_trial_task(trial_id="t-adv", prepared_trial=prepared_trial)
    assert _snapshot(canonical) == before


def test_adversarial_is_not_an_analysis_kind():
    """It runs in the task's own environment, not the analysis container."""
    from oddish.workers.analysis_trials import ANALYSIS_TRIAL_KINDS, is_analysis_kind

    assert ADVERSARIAL_TRIAL_KIND not in ANALYSIS_TRIAL_KINDS
    assert not is_analysis_kind(ADVERSARIAL_TRIAL_KIND)


@pytest.mark.asyncio
async def test_attack_execution_and_experiment_come_from_the_request(attack_task):
    from oddish.schemas import TrialSpec
    task_id, version_id, _ = attack_task
    async with get_session() as session:
        parent = (await session.get(TrialModel, f"{task_id}-solver")).experiment_id
    result = await launch(task_id, version_id, experiment_id=parent,
        execution=TrialSpec(agent="codex", model="openai/gpt-5.6-sol", environment="modal"),
        max_trial_attempts=2)
    async with get_session() as session:
        trial = await session.get(TrialModel, result["trial_id"])
        assert trial.experiment_id == parent
        assert trial.agent == "codex"
        assert trial.model == "openai/gpt-5.6-sol"
        assert trial.environment == "modal"
        assert trial.max_attempts == 2
        assert trial.timeout_minutes is None


@pytest.mark.asyncio
async def test_unlinked_experiment_is_rejected_without_creating_an_attack(attack_task):
    task_id, version_id, _ = attack_task
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id, experiment_id="another-experiment")
    assert error.value.status_code == 404
    assert await attack_trials(task_id) == []


@pytest.mark.asyncio
async def test_trial_listing_only_exposes_attack_evidence_when_requested(attack_task):
    from oddish.core.sharing.helpers import list_task_trials_for_task
    task_id, version_id, _ = attack_task
    first = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(first)
    second = (await launch(task_id, version_id, request_id="second"))["trial_id"]
    async with get_session() as session:
        (await session.get(TrialModel, first)).superseded_by_trial_id = second
    async with get_session() as session:
        ordinary = await list_task_trials_for_task(session, task_id, version=1)
        evidence = await list_task_trials_for_task(session, task_id, version=1, include_adversarial=True)
    assert all(trial.kind == "agent" for trial in ordinary)
    assert {trial.id for trial in evidence if trial.kind == "adversarial"} == {
        first,
        second,
        f"{first}-replay",
    }


def test_attack_prefix_does_not_reparse_a_historical_configuration(tmp_path, monkeypatch):
    from oddish.workers.queue.trial_handler import _stage_adversarial_task
    task = tmp_path / "task"
    _write_task(task)
    before = _snapshot(task)
    monkeypatch.setattr("oddish.cli.api.compute_task_content_hash", lambda *args, **kwargs: pytest.fail("Runtime reparsed task configuration"))
    _stage_adversarial_task(task, prompt_text="ADVERSARIAL REVIEW OF THIS TASK\n")
    assert (task / "instruction.md").read_text().endswith(before["instruction.md"].decode())
    assert {k:v for k,v in _snapshot(task).items() if k != "instruction.md"} == {k:v for k,v in before.items() if k != "instruction.md"}


@pytest.mark.asyncio
async def test_task_page_resolves_experiment_for_exact_version_without_picker(attack_task):
    task_id, version_id, _ = attack_task
    async with get_session() as session:
        parent = await session.get(TrialModel, f"{task_id}-solver")
        wanted = parent.experiment_id
        other = ExperimentModel(name=task_id + "-other-version")
        shadow = ExperimentModel(name=task_id + "-qa-report", shadow_of=wanted)
        session.add_all([other, shadow])
        await session.flush()
        for experiment, run_version, kind in [(other, None, "agent"), (shadow, version_id, "qa")]:
            await session.execute(text("INSERT INTO task_experiments (task_id, experiment_id, created_at) VALUES (:task, :experiment, NOW())"), {"task": task_id, "experiment": experiment.id})
            session.add(TrialModel(id=uuid.uuid4().hex, name="other-run", task_id=task_id,
                task_version_id=run_version, experiment_id=experiment.id,
                agent="codex", model="openai/gpt-6-luna", provider="openai",
                queue_key="openai/gpt-6-luna", kind=kind, status=TrialStatus.SUCCESS,
                reward=1.0, created_at=utcnow() + timedelta(seconds=1)))
    result = await launch(task_id, version_id)
    async with get_session() as session:
        attack = await session.get(TrialModel, result["trial_id"])
        assert attack.task_version_id == version_id
        assert attack.experiment_id == wanted


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,model", [("claude-code", "claude-sonnet-5-5"), ("codex", "openai/gpt-6-luna"), ("gemini-cli", "gemini/gemini-3-pro-preview")])
async def test_attack_uses_shared_trial_model_routing(attack_task, agent, model):
    from oddish.core.sweeps import resolve_trial_model
    from oddish.config import settings
    task_id, version_id, _ = attack_task
    execution = TrialSpec(agent=agent, model=model, agent_config={"kwargs": {"reasoning_effort": "high"}})
    result = await launch(task_id, version_id, execution=execution)
    async with get_session() as session:
        row = await session.get(TrialModel, result["trial_id"])
        assert row.model == resolve_trial_model(execution)
        assert row.agent == agent
        assert row.provider == settings.get_provider_for_trial(agent, row.model)
        assert row.queue_key == settings.get_queue_key_for_trial(agent, row.model)
        assert row.harbor_config["agent_config"]["kwargs"]["reasoning_effort"] == "high"


def test_default_audit_routes_to_openrouter_glm_with_supported_harness():
    from oddish.config import settings
    from oddish.core.adversarial import ADVERSARIAL_MODEL
    from oddish.core.sweeps import resolve_trial_model

    execution = TrialSpec(agent="auto", model=ADVERSARIAL_MODEL)
    model = resolve_trial_model(execution)
    assert model == "openrouter/z-ai/glm-5.3"
    assert execution.agent == "mini-swe-agent"
    assert settings.get_provider_for_trial(execution.agent, model) == "openrouter"
    assert settings.get_queue_key_for_trial(execution.agent, model) == model
    from oddish.workers.harbor.agent_config import _build_agent_config
    from oddish.workers.harbor.model_hosts import outbound_hosts_for_model
    routed = _build_agent_config(agent=execution.agent, model=model, raw_harbor_config={"agent_config": execution.agent_overrides()})
    assert routed.model_name == model
    assert routed.import_path == "oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent"
    assert outbound_hosts_for_model(model) == ["openrouter.ai"]


@pytest.mark.asyncio
async def test_default_audit_model_is_independent_of_expensive_source_and_inherits_environment(attack_task, monkeypatch):
    from oddish.config import settings
    task_id, version_id, _ = attack_task
    monkeypatch.setenv("ODDISH_ADVERSARIAL_MODEL", "openai/gpt-6-luna")
    async with get_session() as session:
        solver = await session.get(TrialModel, f"{task_id}-solver")
        solver.model = "anthropic/claude-opus-5"
        solver.provider = "anthropic"
        solver.created_at = utcnow() + timedelta(seconds=1)
        solver.environment = "archil"
        solver.harbor_config = {"docker_image": "registry/task@sha256:abc", "environment": {"override_cpus": 4}, "agent_config": {"env": {"ANTHROPIC_API_KEY": "must-not-copy"}, "kwargs": {"reasoning_effort": "max"}}}
    result = await launch(task_id, version_id, execution=None)
    async with get_session() as session:
        row = await session.get(TrialModel, result["trial_id"])
        assert row.agent == "mini-swe-agent"
        assert row.model == "openrouter/z-ai/glm-5.3"
        assert row.provider == "openrouter"
        assert row.queue_key == "openrouter/z-ai/glm-5.3"
        assert row.environment == "archil"
        assert row.harbor_config["docker_image"] == "registry/task@sha256:abc"
        assert row.harbor_config["environment"] == {"override_cpus": 4}
        assert "ANTHROPIC_API_KEY" not in str(row.harbor_config)
        assert "reasoning_effort" not in row.harbor_config["agent_config"].get("kwargs", {})
        assert row.harbor_config["agent_config"]["kwargs"]["version"] == settings.harness_versions["mini-swe-agent"]
        assert row.harbor_config["adversarial"]["execution"]["source_trial_id"] == solver.id


@pytest.mark.asyncio
async def test_default_audit_queues_without_model_history(attack_task):
    task_id, version_id, _ = attack_task
    result = await launch(task_id, version_id, execution=None)
    async with get_session() as session:
        row = await session.get(TrialModel, result["trial_id"])
        assert row.agent == "mini-swe-agent"
        assert row.model == "openrouter/z-ai/glm-5.3"
        assert row.provider == "openrouter"
        assert row.queue_key == "openrouter/z-ai/glm-5.3"


@pytest.mark.asyncio
async def test_attack_rejects_provider_harness_mismatch_before_queueing(attack_task):
    task_id, version_id, _ = attack_task
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id, execution=TrialSpec(agent="codex", model="hunyuan/hy4-preview-offline"))
    assert error.value.status_code == 400
    assert "only on mini-swe-agent" in error.value.detail
    assert await attack_trials(task_id) == []


@pytest.mark.asyncio
async def test_ordinary_and_adversarial_admission_store_same_model_and_harness(attack_task):
    from oddish.queue import append_trials_to_task
    from oddish.schemas import TaskSubmission
    task_id, version_id, _ = attack_task
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        solver = await session.get(TrialModel, f"{task_id}-solver")
        rows = await append_trials_to_task(
            session, task=task, experiment_id=solver.experiment_id,
            submission=TaskSubmission(task_path="p", trials=[TrialSpec(agent="auto", model="openai/gpt-6-luna")]),
        )
        ordinary = rows[0]
        ordinary_id = ordinary.id
    result = await launch(task_id, version_id, execution=TrialSpec(agent="auto", model="openai/gpt-6-luna"))
    async with get_session() as session:
        ordinary = await session.get(TrialModel, ordinary_id)
        attack = await session.get(TrialModel, result["trial_id"])
        assert ordinary.agent == attack.agent == "codex"
        assert ordinary.model == attack.model == "openai/gpt-6-luna"
        assert ordinary.provider == attack.provider
        assert ordinary.queue_key == attack.queue_key
        assert ordinary.harbor_config["agent_config"] == attack.harbor_config["agent_config"]


def test_prefixing_local_instruction_symlink_does_not_modify_its_target(tmp_path):
    from oddish.workers.queue.trial_handler import _stage_adversarial_task
    source = tmp_path / "original.md"
    source.write_text("Original instruction")
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").symlink_to(source)
    _stage_adversarial_task(task, prompt_text="ADVERSARIAL REVIEW OF THIS TASK\n")
    assert source.read_text() == "Original instruction"
    assert (task / "instruction.md").read_text().endswith("Original instruction")
    assert not (task / "instruction.md").is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.PAUSED,
        TrialStatus.RETRYING,
    ],
)
async def test_manual_qa_ignores_an_active_attack(attack_task, state, monkeypatch):
    from oddish.core.endpoints import qa

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    async with get_session() as session:
        (await session.get(TrialModel, attack_id)).status = state
        (await session.get(TaskModel, task_id)).verdict = {"is_good": True}
    monkeypatch.setattr(qa, "qa_source_evidence_errors", AsyncMock(return_value=()))
    async with get_session() as session:
        result = await qa.backfill_task_analysis_core(
            session, task_id=task_id, force=True
        )
    assert result["status"] == "queued"
    [run] = await qa_trials(task_id)
    assert attack_id not in str(qa_payload(run))


@pytest.mark.asyncio
async def test_launch_budget_rejection_precedes_trial_and_verdict_writes(
    attack_task, monkeypatch
):
    from oddish.core import quota_admission

    task_id, version_id, _ = attack_task
    reject = AsyncMock(side_effect=HTTPException(402, "budget exhausted"))
    monkeypatch.setattr(quota_admission, "admit_organization_qa_trials", reject)
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id, requested_by_user_id="requester")
    assert error.value.status_code == 402
    assert await attack_trials(task_id) == []
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.verdict == {"is_good": True}
    assert reject.await_args.args[1] is None
    assert reject.await_args.kwargs == {"count": 1}


@pytest.mark.asyncio
async def test_duplicate_attack_does_not_readmit_budget(attack_task, monkeypatch):
    from oddish.core import quota_admission

    task_id, version_id, _ = attack_task
    first = await launch(task_id, version_id)
    reject = AsyncMock(side_effect=HTTPException(402, "budget exhausted"))
    monkeypatch.setattr(quota_admission, "admit_organization_qa_trials", reject)
    assert (await launch(task_id, version_id))["trial_id"] == first["trial_id"]
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_attack_prompt_omits_audit_findings(attack_task):
    """Black box: the attacker knows only what any solver of the task knows."""
    from importlib import resources

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        version.pre_trial = {
            "items": [
                {
                    "dimension": "verifier",
                    "file": "tests/test.sh",
                    "line_start": 3,
                    "line_end": 3,
                    "title": "Grader leaves agent processes running",
                    "detail": "Only named processes are killed before grading.",
                }
            ]
        }
    await launch(task_id, version_id)
    [trial] = await attack_trials(task_id)
    prompt = trial.harbor_config["adversarial"]["prompt_text"]
    assert prompt == attack_prompt(
        resources.files("oddish.analyze")
        .joinpath("adversarial_prompt.txt")
        .read_text(encoding="utf-8"),
        0,
    )[0]
    assert "Grader leaves agent processes running" not in prompt
    assert "tests/test.sh" not in prompt
    assert "audit" not in prompt.lower()


@pytest.mark.asyncio
async def test_prompt_snapshot_survives_resource_change_and_retry(
    attack_task, monkeypatch, tmp_path
):
    import hashlib
    import shutil
    from importlib import resources
    from oddish.workers.queue.trial_handler import _prepare_trial_task
    from oddish.workers.queue.trial_steps import PreparedTrialRun

    task_id, version_id, _ = attack_task
    prompt_file = tmp_path / "adversarial_prompt.txt"
    prompt_file.write_bytes(b"Prompt A\r\n")
    original_files = resources.files
    monkeypatch.setattr(
        resources,
        "files",
        lambda anchor: (
            tmp_path if anchor == "oddish.analyze" else original_files(anchor)
        ),
    )
    first = await launch(task_id, version_id)
    [trial] = await attack_trials(task_id)
    pins = trial.harbor_config["adversarial"]
    assert pins["prompt_text"] == "Prompt A\r\n"
    assert pins["prompt_sha256"] == hashlib.sha256(b"Prompt A\r\n").hexdigest()
    prompt_file.write_bytes(b"Prompt B\n")
    assert (await launch(task_id, version_id))["trial_id"] == first["trial_id"]
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        prepared_trial = PreparedTrialRun(
            task_path=version.task_path,
            task_s3_key=None,
            task_id=task_id,
            trial_agent=trial.agent,
            trial_model=trial.model,
            trial_environment=None,
            trial_harbor_config=trial.harbor_config,
            trial_kind="adversarial",
        )
    for _ in range(2):
        prepared = await _prepare_trial_task(
            trial_id=trial.id, prepared_trial=prepared_trial
        )
        try:
            assert (
                (prepared.task_path / "instruction.md")
                .read_bytes()
                .startswith(b"Prompt A\r\n")
            )
        finally:
            shutil.rmtree(prepared.temp_task_dir)
    await settle_attack(trial.id)
    second = await launch(task_id, version_id, request_id="new")
    async with get_session() as session:
        assert (await session.get(TrialModel, second["trial_id"])).harbor_config[
            "adversarial"
        ]["prompt_text"] == "Prompt B\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block,message",
    [
        ({}, "snapshot is missing"),
        ({"prompt_text": "", "prompt_sha256": "x"}, "snapshot is missing"),
        ({"prompt_text": "saved", "prompt_sha256": 42}, "snapshot is missing"),
        ({"prompt_text": "tampered", "prompt_sha256": "0" * 64}, "hash mismatch"),
    ],
)
async def test_preparation_rejects_missing_or_corrupt_prompt(tmp_path, block, message):
    from oddish.workers.queue.trial_handler import _prepare_trial_task
    from oddish.workers.queue.trial_steps import PreparedTrialRun

    canonical = tmp_path / "canonical"
    _write_task(canonical)
    before = _snapshot(canonical)
    prepared = PreparedTrialRun(
        task_path=str(canonical),
        task_s3_key=None,
        task_id="t",
        trial_agent="codex",
        trial_model="m",
        trial_environment=None,
        trial_harbor_config={"adversarial": block},
        trial_kind="adversarial",
    )
    with pytest.raises(ValueError, match=message):
        await _prepare_trial_task(trial_id="t", prepared_trial=prepared)
    assert _snapshot(canonical) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "content_hash",
        "task_path",
        "task_s3_key",
        "current_version_id",
        "deleted_at",
    ],
)
async def test_archive_transfer_releases_task_lock_and_rechecks_source(
    attack_task, monkeypatch, mutation
):
    from oddish.core.endpoints import qa
    from oddish.db.storage import PinnedTaskArchive

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (await session.get(TaskVersionModel, version_id)).task_s3_key = "tasks/source/"
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot_key = "tasks/source/.oddish-snapshots/unique/.oddish-task.tar.gz"

    async def pin(_):
        entered.set()
        await release.wait()
        return PinnedTaskArchive(
            {"archive_key": snapshot_key, "etag": "etag"}, snapshot_key
        )

    from types import SimpleNamespace

    storage = SimpleNamespace(pin_task_archive=pin, delete_object=AsyncMock())
    monkeypatch.setattr(qa, "get_storage_client", lambda: storage)
    pending = asyncio.create_task(launch(task_id, version_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with get_session() as session:
            # NOWAIT raises if the launch still owns this row lock.
            task = await session.scalar(
                select(TaskModel)
                .where(TaskModel.id == task_id)
                .with_for_update(nowait=True)
            )
            version = await session.get(
                TaskVersionModel, version_id, with_for_update=True
            )
            if mutation in ("content_hash", "task_path", "task_s3_key"):
                setattr(version, mutation, "changed")
            elif mutation == "current_version_id":
                task.current_version_id = None
            elif mutation == "deleted_at":
                task.deleted_at = utcnow()
        release.set()
        if mutation:
            with pytest.raises(HTTPException) as error:
                await pending
            assert error.value.status_code == 409
            assert (
                error.value.detail == "Task source changed; refresh the task and retry"
            )
            assert await attack_trials(task_id) == []
            storage.delete_object.assert_awaited_once_with(snapshot_key)
        else:
            assert (await pending)["status"] == "queued"
            storage.delete_object.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_racing_snapshots_delete_only_the_losing_upload(attack_task, monkeypatch):
    from oddish.core.endpoints import qa
    from oddish.db.storage import PinnedTaskArchive
    from types import SimpleNamespace

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (await session.get(TaskVersionModel, version_id)).task_s3_key = "tasks/source/"
    both_entered = asyncio.Event()
    keys = []

    async def pin(_):
        key = f"tasks/source/.oddish-snapshots/{len(keys)}/.oddish-task.tar.gz"
        keys.append(key)
        if len(keys) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 5)
        return PinnedTaskArchive({"archive_key": key, "etag": "etag"}, key)

    storage = SimpleNamespace(pin_task_archive=pin, delete_object=AsyncMock())
    monkeypatch.setattr(qa, "get_storage_client", lambda: storage)
    first, second = await asyncio.gather(
        launch(task_id, version_id, "one"), launch(task_id, version_id, "two")
    )
    assert first["trial_id"] == second["trial_id"]
    [trial] = await attack_trials(task_id)
    winner = trial.harbor_config["task_artifact"]["archive_key"]
    storage.delete_object.assert_awaited_once_with(
        next(key for key in keys if key != winner)
    )
    async with get_session() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM worker_jobs WHERE subject_id = :id"),
                {"id": trial.id},
            )
            == 1
        )
        assert (
            await session.get(TaskVersionModel, version_id)
        ).task_s3_key == winner.rsplit("/", 1)[0] + "/"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_qa", [False, True])
@pytest.mark.parametrize("scope", ["user", "organization", "missing_requester"])
async def test_adversarial_qa_admission_uses_only_organization_budget(
    attack_task, monkeypatch, scope, include_qa
):
    from decimal import Decimal
    from oddish.config import QuotaMode, settings
    from oddish.core import quota_admission
    task_id, version_id, _ = attack_task
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "quota_counts_analysis_and_compute", include_qa)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.25"))
    user_limit = AsyncMock(
        side_effect=AssertionError("QA must not check personal limits")
    )
    monkeypatch.setattr(quota_admission, "get_effective_limit", user_limit)
    monkeypatch.setattr(
        quota_admission,
        "get_effective_org_limit",
        AsyncMock(return_value=Decimal("0") if scope == "organization" else None),
    )
    async with get_session() as session:
        (await session.get(TaskModel, task_id)).org_id = task_id
    requester = None if scope == "missing_requester" else "requester"
    if scope == "organization" and include_qa:
        with pytest.raises(HTTPException) as error:
            await launch(task_id, version_id, requested_by_user_id=requester)
        assert error.value.status_code == 402
        assert await attack_trials(task_id) == []
        async with get_session() as session:
            assert (await session.get(TaskModel, task_id)).verdict == {"is_good": True}
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM worker_jobs WHERE subject_id LIKE :id"),
                    {"id": task_id + "%"},
                )
                == 0
            )
    else:
        result = await launch(task_id, version_id, requested_by_user_id=requester)
        assert result["status"] == "queued"
        [trial] = await attack_trials(task_id)
        assert trial.kind == "adversarial"
        assert trial.org_id == task_id
        assert trial.billed_user_id is None
        assert trial.harbor_config["adversarial"]["requested_by_user_id"] == requester
    user_limit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["solver", "baselines", "none"])
async def test_launch_selects_latest_nonbaseline_then_baseline_then_default(
    attack_task, history
):
    task_id, version_id, _ = attack_task
    expected_source = None
    async with get_session() as session:
        rows = list(
            await session.scalars(
                select(TrialModel).where(TrialModel.task_id == task_id)
            )
        )
        for index, row in enumerate(rows):
            row.created_at = utcnow() + timedelta(seconds=index)
            if row.agent == "claude-code":
                row.created_at = utcnow() - timedelta(days=1)
                expected_source = row.id
                if history != "solver":
                    row.deleted_at = utcnow()
            else:
                row.agent = (
                    "agent-oracle-custom" if row.agent == "oracle" else "nop-custom"
                )
            if history == "none":
                row.deleted_at = utcnow()
        if history == "baselines":
            expected_source = max(
                (r for r in rows if r.deleted_at is None),
                key=lambda r: (r.created_at, r.id),
            ).id
        if history == "none":
            expected_source = None
    result = await launch(task_id, version_id)
    async with get_session() as session:
        trial = await session.get(TrialModel, result["trial_id"])
        assert (
            trial.harbor_config["adversarial"]["execution"]["source_trial_id"]
            == expected_source
        )


@pytest.mark.asyncio
async def test_source_rejection_never_deletes_preexisting_archive(
    attack_task, monkeypatch
):
    from oddish.core.endpoints import qa
    from oddish.db.storage import PinnedTaskArchive
    from types import SimpleNamespace

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (
            await session.get(TaskVersionModel, version_id)
        ).task_s3_key = "tasks/existing/"

    async def pin(_):
        async with get_session() as session:
            (await session.get(TaskVersionModel, version_id)).content_hash = "changed"
        return PinnedTaskArchive(
            {"archive_key": "tasks/existing/archive", "etag": "etag"}
        )

    storage = SimpleNamespace(pin_task_archive=pin, delete_object=AsyncMock())
    monkeypatch.setattr(qa, "get_storage_client", lambda: storage)
    with pytest.raises(HTTPException) as error:
        await launch(task_id, version_id)
    assert error.value.status_code == 409
    storage.delete_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_adversarial_qa_cost_is_reported_separately_from_solver_spend(
    attack_task,
):
    from oddish.core.admin import _qa_cost_time_series
    from oddish.core.endpoints.qa_cost import get_task_qa_costs
    from oddish.core.quotas import sum_cost_usd

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (await session.get(TaskModel, task_id)).org_id = task_id
    result = await launch(task_id, version_id, requested_by_user_id="requester")
    await settle_attack(result["trial_id"])
    async with get_session() as session:
        trial = await session.get(TrialModel, result["trial_id"])
        trial.cost_usd = 0.5
        await session.flush()
        assert trial.billed_user_id is None
        costs = await get_task_qa_costs(session, task_ids=[task_id], org_id=task_id)
        assert costs[task_id].qa_cost_usd == 0.5
        _, series = await _qa_cost_time_series(
            session, since=None, bucket="day", org_id=task_id
        )
        assert [(key.key, key.label) for key in series.keys] == [
            ("adversarial", "Adversarial QA")
        ]
        assert sum(bucket.cost_usd for bucket in series.buckets) == 0.5
        assert (
            await sum_cost_usd(
                session, task_id, "requester", utcnow() - timedelta(hours=1)
            )
            == 0
        )


# --- exploit replay ---------------------------------------------------------


@pytest.mark.asyncio
async def test_one_launch_queues_several_attacks(attack_task, monkeypatch):
    from oddish.core.adversarial import ATTACKS_PER_LAUNCH

    monkeypatch.setattr(
        "oddish.core.endpoints.qa.ATTACKS_PER_LAUNCH", ATTACKS_PER_LAUNCH
    )
    task_id, version_id, _ = attack_task
    result = await launch(task_id, version_id)
    attacks = await attack_trials(task_id)
    assert ATTACKS_PER_LAUNCH == 4
    assert sorted(result["trial_ids"]) == sorted(t.id for t in attacks)
    by_id = {t.id: t.harbor_config["adversarial"] for t in attacks}
    pins = [by_id[i] for i in result["trial_ids"]]
    assert [p["white_box"] for p in pins] == [False, True] * 2
    starts = [p["prompt_text"].split("Start with idea ")[1][0] for p in pins]
    assert starts == ["2", "2", "3", "3"]
    for p in pins:
        assert ("cannot see the grader" in p["prompt_text"]) is not p["white_box"]
        assert "{" not in p["prompt_text"]
    again = await launch(task_id, version_id)
    assert again["status"] == "existing"
    assert (again["trial_id"], again["trial_ids"]) == (
        result["trial_id"],
        result["trial_ids"],
    )
    other = await launch(task_id, version_id, request_id="req-2")
    assert other["status"] == "already_running"
    assert other["trial_ids"] == result["trial_ids"]
    assert len(await attack_trials(task_id)) == ATTACKS_PER_LAUNCH


def test_white_box_attack_gets_grader_code_but_no_data(tmp_path):
    from oddish.workers.queue.trial_handler import _stage_adversarial_task

    for white_box in (False, True):
        root = tmp_path / str(white_box)
        _write_task(root)
        (root / "tests" / "cases.json").write_text('{"expected": "SECRET"}')
        (root / "tests" / "big.py").write_text("x" * 33_000)
        (root / "tests" / "small.py").write_text("y = 1\n")
        _stage_adversarial_task(root, prompt_text="ATTACK\n", white_box=white_box)
        staged = (root / "instruction.md").read_text()
        assert staged.startswith("ATTACK\nWrite a function that adds numbers.\n")
        assert ("=== tests/test.sh\n#!/bin/sh" in staged) is white_box
        assert ("tests/big.py (33000 bytes, left out for length" in staged) is white_box
        # test.sh first, then smallest first: small.py fits before big.py.
        assert ("=== tests/small.py\ny = 1" in staged) is white_box
        if white_box:
            assert staged.index("tests/test.sh") < staged.index("tests/small.py")
        assert "SECRET" not in staged and "x" * 100 not in staged


@pytest.mark.parametrize(
    "script, expected",
    [
        (None, False),
        ("", False),
        ("exit 0\n", False),
        ("#!/bin/sh\n# nothing found\nexit 0\n", False),
        ("#!/bin/sh\nprintf 'x' > /app/answer.py\n", True),
    ],
)
def test_only_a_script_that_does_something_is_replayed(script, expected):
    from oddish.core.adversarial import has_exploit

    assert has_exploit(script) is expected


@pytest.mark.asyncio
async def test_an_attack_queues_one_replay_of_its_current_attempt(attack_task):
    from oddish.core.adversarial import queue_exploit_replay

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(attack_id, confirmed=False)
    script = "#!/bin/sh\nprintf 'x' > /app/answer.py\n"
    for _ in range(2):
        async with get_session() as session:
            task = await session.get(TaskModel, task_id, with_for_update=True)
            attack = await session.get(TrialModel, attack_id)
            await queue_exploit_replay(session, task=task, attack=attack, script=script)
    [replay] = await replay_trials(task_id)
    pins = replay.harbor_config[ADVERSARIAL_TRIAL_KIND]
    assert pins["replay_of"] == attack_id
    assert pins["attack_attempt"] == 1
    assert pins["exploit_text"] == script
    assert pins["exploit_sha256"] == hashlib.sha256(script.encode()).hexdigest()
    assert "agent_config" not in replay.harbor_config
    assert "prompt_text" not in pins
    assert replay.status == TrialStatus.QUEUED
    async with get_session() as session:
        job = await session.scalar(
            text(
                "SELECT status::text FROM worker_jobs WHERE kind::text = 'TRIAL' "
                "AND subject_id = :id"
            ),
            {"id": replay.id},
        )
    assert job == "QUEUED"


def test_a_replay_runs_the_exploit_as_the_only_solution(tmp_path):
    from oddish.core.adversarial import execution_agent, REPLAY_AGENT
    from oddish.workers.queue.trial_handler import _stage_exploit_replay

    _write_task(tmp_path)
    script = "#!/bin/sh\nprintf 'x' > /app/answer.py\n"
    pins = {
        "exploit_text": script,
        "exploit_sha256": hashlib.sha256(script.encode()).hexdigest(),
    }
    _stage_exploit_replay(tmp_path, pins=pins)
    assert sorted(p.name for p in (tmp_path / "solution").iterdir()) == [
        "exploit.sh",
        "protected.sha256",
        "solve.sh",
    ]
    assert (tmp_path / "solution" / "exploit.sh").read_text() == script
    assert execution_agent(REPLAY_AGENT) == "oracle"
    assert execution_agent("mini-swe-agent") == "mini-swe-agent"
    with pytest.raises(ValueError, match="does not match its hash"):
        _stage_exploit_replay(tmp_path, pins={**pins, "exploit_sha256": "0" * 64})


def test_an_attack_refuses_multi_step_tasks_before_running(tmp_path):
    from oddish.workers.queue.trial_handler import _stage_adversarial_task

    _write_task(tmp_path)
    (tmp_path / "task.toml").write_text(
        'version = "1.0"\n[[steps]]\nname = "one"\n[[steps]]\nname = "two"\n'
    )
    with pytest.raises(ValueError, match="single-step"):
        _stage_adversarial_task(tmp_path, prompt_text="ATTACK\n")


def test_a_replay_refuses_multi_step_tasks(tmp_path):
    from oddish.workers.queue.trial_handler import _stage_exploit_replay

    _write_task(tmp_path)
    (tmp_path / "task.toml").write_text(
        'version = "1.0"\n[[steps]]\nname = "one"\n[[steps]]\nname = "two"\n'
    )
    for step in ("one", "two"):
        (tmp_path / "steps" / step / "solution").mkdir(parents=True)
        (tmp_path / "steps" / step / "instruction.md").write_text("Step.\n")
    script = "#!/bin/sh\ntrue\n"
    with pytest.raises(ValueError, match="single-step"):
        _stage_exploit_replay(
            tmp_path,
            pins={
                "exploit_text": script,
                "exploit_sha256": hashlib.sha256(script.encode()).hexdigest(),
            },
        )
    assert (tmp_path / "solution" / "solve.sh").read_text() == "#!/bin/sh\ntrue\n"


def test_protected_hashes_skip_given_and_tiny_files(tmp_path):
    from oddish.core.adversarial import protected_hashes

    _write_task(tmp_path)
    data = "x" * 100
    (tmp_path / "tests" / "cases.json").write_text(data)
    (tmp_path / "tests" / "shared.csv").write_text("y" * 100)
    (tmp_path / "environment" / "shared.csv").write_text("y" * 100)
    (tmp_path / "solution" / "answer.py").write_text("z" * 100)
    names = [line.split("  ", 1)[1] for line in protected_hashes(tmp_path).splitlines()]
    # test.sh and solve.sh are under 64 bytes; shared.csv is given to the solver.
    assert sorted(names) == ["solution/answer.py", "tests/cases.json"]


def test_the_replay_wrapper_lists_copied_protected_files_and_keeps_the_exit_code(
    tmp_path,
):
    import subprocess

    from oddish.core.adversarial import (
        REPLAY_SOLVE_SH,
        protected_hashes,
        write_leak_check,
    )

    task = tmp_path / "task"
    _write_task(task)
    (task / "tests" / "cases.json").write_text("x" * 100)
    logs = tmp_path / "job" / "trial-1" / "logs"
    solution = tmp_path / "solution"
    solution.mkdir()
    (solution / "protected.sha256").write_text(protected_hashes(task))
    (solution / "exploit.sh").write_text(
        f"#!/bin/sh\nmkdir -p {logs}/agent/leaked/sub\n"
        f"printf '%100s' '' | tr ' ' x > {logs}/agent/leaked/sub/copy.json\n"
        f"echo unrelated > {logs}/agent/leaked/other.txt\nexit 3\n"
    )
    (solution / "solve.sh").write_text(REPLAY_SOLVE_SH.replace("/logs", str(logs)))
    done = subprocess.run(["sh", str(solution / "solve.sh")], capture_output=True)
    assert done.returncode == 3
    # Only sh and mv ran in the image; the worker hashes the snapshot.
    assert not (logs / "agent" / "leaked").exists()
    write_leak_check(tmp_path / "job", (solution / "protected.sha256").read_text())
    assert (logs / "agent" / "leak-check.txt").read_text() == "tests/cases.json\n"


@pytest.mark.asyncio
async def test_an_attack_on_a_failed_task_still_replays_its_exploit(
    attack_task, monkeypatch
):
    from oddish.workers.queue import trial_handler

    task_id, version_id, _ = attack_task
    async with get_session() as session:
        (await session.get(TaskModel, task_id)).status = TaskStatus.FAILED
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(attack_id, confirmed=False)
    monkeypatch.setattr(
        trial_handler,
        "read_exploit_script",
        AsyncMock(return_value="#!/bin/sh\ntrue\n"),
    )
    await trial_handler._run_post_trial_hooks(attack_id)
    assert len(await replay_trials(task_id)) == 1


@pytest.mark.asyncio
async def test_the_settle_hook_replays_the_exploit_and_a_pass_leaves_the_verdict(
    attack_task, monkeypatch
):
    from oddish.workers.queue import trial_handler

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(attack_id, confirmed=False)
    script = "#!/bin/sh\nprintf 'x' > /app/answer.py\n"
    monkeypatch.setattr(
        trial_handler, "read_exploit_script", AsyncMock(return_value=script)
    )
    await trial_handler._run_post_trial_hooks(attack_id)
    [replay] = await replay_trials(task_id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
    # The attack alone changes nothing about the published verdict.
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict == {"is_good": True}

    async with get_session() as session:
        row = await session.get(TrialModel, replay.id)
        row.status = TrialStatus.SUCCESS
        row.reward = 1.0
        row.attempts = 1
        row.finished_at = utcnow()
    monkeypatch.setattr(
        trial_handler, "read_leak_check", AsyncMock(return_value=["tests/cases.json"])
    )
    for _ in range(2):
        await trial_handler._run_post_trial_hooks(replay.id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        version = await session.get(TaskVersionModel, version_id)
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict == {"is_good": True}
    assert await qa_trials(task_id) == []
    # The replay records must-fix findings once, however often the hook runs.
    findings = [r["finding"] for r in version.reported_findings]
    assert [(f["dimension"], f["tier"], f["file"]) for f in findings] == [
        ("verifier", "must_fix", "tests/test.sh"),
        ("info_leakage", "must_fix", "tests/cases.json"),
    ]
    assert {f["replay_trial_id"] for f in findings} == {replay.id}
    assert {f["attack_trial_id"] for f in findings} == {attack_id}




@pytest.mark.asyncio
async def test_the_cleanup_sweep_reruns_failed_replay_hooks(attack_task, monkeypatch):
    from oddish.workers.queue import cleanup, trial_handler

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    await settle_attack(attack_id, confirmed=False)
    script = "#!/bin/sh\nprintf 'x' > /app/answer.py\n"
    down = AsyncMock(side_effect=RuntimeError("storage down"))
    monkeypatch.setattr(trial_handler, "read_exploit_script", down)
    await trial_handler._run_post_trial_hooks(attack_id)
    assert await replay_trials(task_id) == []
    monkeypatch.setattr(
        trial_handler, "read_exploit_script", AsyncMock(return_value=script)
    )
    await cleanup._rerun_adversarial_hooks()
    [replay] = await replay_trials(task_id)

    async with get_session() as session:
        row = await session.get(TrialModel, replay.id)
        row.status = TrialStatus.FAILED
        row.reward = 0.0
        row.attempts = 1
        row.finished_at = utcnow()
    monkeypatch.setattr(trial_handler, "read_leak_check", down)
    await trial_handler._run_post_trial_hooks(replay.id)
    monkeypatch.setattr(
        trial_handler, "read_leak_check", AsyncMock(return_value=["tests/cases.json"])
    )
    await cleanup._rerun_adversarial_hooks()
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
    assert [r["finding"]["dimension"] for r in version.reported_findings] == [
        "info_leakage"
    ]


def test_leak_findings_keep_distinct_ids_for_long_shared_prefixes():
    deep = "tests/fixtures/expected_outputs/generated/"
    replay = TrialModel(id="replay-1", status=TrialStatus.FAILED, reward=0)
    found = replay_findings(replay, [deep + "case_a.json", deep + "case_b.json"])
    assert len({f["id"] for f in found}) == 2


@pytest.mark.asyncio
async def test_task_status_views_leave_out_attacks_and_replays(attack_task):
    from sqlalchemy.orm import selectinload

    from oddish.core.endpoints.tasks_query import _load_task_status_trials
    from oddish.core.helpers import get_task_status_trials

    task_id, version_id, _ = attack_task
    attack_id = (await launch(task_id, version_id))["trial_id"]
    # A passing replay has reward 1; it must not count as a solved attempt.
    await settle_attack(attack_id)
    async with get_session() as session:
        task = await session.scalar(
            select(TaskModel)
            .options(selectinload(TaskModel.trials))
            .where(TaskModel.id == task_id)
        )
        assert {t.kind for t in task.trials} >= {"adversarial"}
        status_trials = get_task_status_trials(task)
        loaded = await _load_task_status_trials(session, task)
    assert all(t.kind != "adversarial" for t in status_trials)
    assert all(t.kind != "adversarial" for t in loaded)
