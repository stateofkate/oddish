"""The backfill restores a version's own verdict from its latest QA run.

v4 was reviewed and accepted; v5 then became the default and replaced the
task's verdict. v4's QA run still has its qa_result.json, so the backfill
rebuilds the verdict that run published and stores it on v4.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from oddish.analyze.models import TaskVerdictModel
from oddish.core.analysis_payload import qa_trial_evidence
from oddish.core.endpoints.task_panel import get_task_panel_core
from oddish.db import TaskStatus, VerdictStatus
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)


def _qa_entry(trial_id: str) -> dict:
    return {
        "trial_id": trial_id,
        "analysis": {
            "trial_name": trial_id,
            "classification": "GOOD_SUCCESS",
            "subtype": "legitimate_solution",
            "evidence": "e",
            "root_cause": "r",
            "recommendation": "x",
            "reward": 1.0,
            "action_items": [],
            "exploitation": [],
        },
        "trajectory_summary": {
            "summary": "The agent edited the file and the verifier agreed.",
            "highlights": [{"step_id": 1, "title": "edit", "why": "it landed"}],
            "components": [
                {
                    "step_ids": [1],
                    "trajectory_component": "implementing",
                    "action": "edit",
                    "purpose": "build",
                    "summary": "One edit.",
                }
            ],
        },
    }


def _trial(task, experiment, version, **fields) -> TrialModel:
    return TrialModel(
        **{
            "id": uuid4().hex,
            "name": fields.get("agent", "trial"),
            "task_id": task.id,
            "task_version_id": version.id,
            "experiment_id": experiment.id,
            "org_id": task.org_id,
            "provider": "test",
            "queue_key": "test",
            "status": TrialStatus.SUCCESS,
            **fields,
        }
    )


def _qa(task, experiment, version, graded, baselines, *, with_verdict=True):
    return _trial(
        task,
        experiment,
        version,
        agent="claude-code",
        kind="qa",
        harbor_config={
            "analysis_payload": {
                "trial_ids": [row.id for row in graded],
                "with_verdict": with_verdict,
                "trial_evidence": [qa_trial_evidence(row) for row in graded],
                "baseline_evidence": [qa_trial_evidence(row) for row in baselines],
            }
        },
    )


async def _seed(session):
    task = TaskModel(
        name=f"backfill-{uuid4().hex}",
        org_id="backfill-org",
        user="tester",
        task_path="s3://task",
        run_analysis=True,
        status=TaskStatus.COMPLETED,
    )
    session.add(task)
    await session.flush()
    v3, v4, v5 = (
        TaskVersionModel(
            id=f"{task.id}-v{n}",
            task_id=task.id,
            version=n,
            task_path=task.task_path,
            content_hash=f"hash-{n}",
        )
        for n in (3, 4, 5)
    )
    kept = {"verdict": "reject", "is_good": False, "_task_version_id": v3.id}
    v3.verdict = kept
    experiment = ExperimentModel(name=f"backfill-{uuid4().hex}", org_id=task.org_id)
    session.add_all([v3, v4, v5, experiment])
    await session.flush()
    agent = _trial(task, experiment, v4, agent="codex", reward=1.0, has_trajectory=True)
    nop = _trial(task, experiment, v4, agent="nop", reward=0.0)
    oracle = _trial(task, experiment, v4, agent="oracle", reward=1.0)
    session.add_all([agent, nop, oracle])
    await session.flush()
    qa = _qa(task, experiment, v4, [agent], [nop, oracle])
    # v3 already keeps a verdict; its QA run must not replace it.
    old_qa = _qa(task, experiment, v3, [], [])
    session.add_all([qa, old_qa])
    task.current_version_id = v5.id
    task.verdict = {"verdict": "reject", "is_good": False, "_task_version_id": v5.id}
    task.verdict_status = VerdictStatus.SUCCESS
    await session.flush()
    artifact = {
        "trials": [_qa_entry(agent.id)],
        "verdict": TaskVerdictModel(
            verdict="accept",
            confidence="high",
            reasoning="v4's agent solved it legitimately and baselines held.",
        ).model_dump(),
    }
    return task, (v3, v4, v5), qa, artifact, kept


@pytest.mark.asyncio
async def test_backfill_restores_an_older_versions_own_verdict(session, monkeypatch):
    from oddish.core import backfill_version_verdicts as backfill_module

    task, (v3, v4, v5), qa, artifact, kept = await _seed(session)
    task_id, org_id, qa_id = task.id, task.org_id, qa.id
    version_ids = {n: v.id for n, v in ((3, v3), (4, v4), (5, v5))}
    await session.commit()

    async def read_artifact(trial, filename):
        return artifact if trial.id == qa_id else None

    monkeypatch.setattr(backfill_module, "read_analysis_artifact", read_artifact)

    async def stored(n):
        return (
            await session.execute(
                select(TaskVersionModel.verdict).where(
                    TaskVersionModel.id == version_ids[n]
                )
            )
        ).scalar_one()

    try:
        dry = await backfill_module.backfill(apply=False)
        assert dry["recovered"] >= 1
        assert await stored(4) is None, "a dry run writes nothing"

        await backfill_module.backfill(apply=True)
        verdict = await stored(4)
        assert verdict["verdict"] == "accept" and verdict["is_good"] is True
        assert verdict["_task_version_id"] == version_ids[4]
        assert verdict["_graded_by"] == qa_id
        assert verdict["_backfilled_at"]
        assert await stored(3) == kept, "an existing verdict is never replaced"
        assert await stored(5) is None, "a version with no QA run stays empty"

        # The drawer now shows v4's own verdict; v5 keeps the task's.
        panel = await get_task_panel_core(
            session, task_id=task_id, version=4, org_id=org_id
        )
        assert panel.task.review_version_matches is True
        assert panel.task.verdict["is_good"] is True
        panel = await get_task_panel_core(
            session, task_id=task_id, version=5, org_id=org_id
        )
        assert panel.task.verdict["is_good"] is False
    finally:
        await session.rollback()
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await session.execute(
            TaskVersionModel.__table__.delete().where(
                TaskVersionModel.task_id == task_id
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_recovery_reports_why_a_version_has_no_verdict(session, monkeypatch):
    from oddish.core import backfill_version_verdicts as backfill_module

    task, (v3, v4, v5), qa, artifact, _ = await _seed(session)
    no_artifact = AsyncMockArtifact(None)
    monkeypatch.setattr(backfill_module, "read_analysis_artifact", no_artifact)
    assert (await backfill_module.recover_version_verdict(session, v5))[0] == (
        "no_qa_run"
    )
    assert (await backfill_module.recover_version_verdict(session, v4))[0] == (
        "no_artifact"
    )
    monkeypatch.setattr(
        backfill_module,
        "read_analysis_artifact",
        AsyncMockArtifact({**artifact, "verdict": None}),
    )
    qa.harbor_config["analysis_payload"]["with_verdict"] = False
    qa.harbor_config = dict(qa.harbor_config)
    await session.flush()
    assert (await backfill_module.recover_version_verdict(session, v4))[0] == (
        "classification_only"
    )


@pytest.mark.asyncio
async def test_a_later_classification_only_run_keeps_the_earlier_verdict(
    session, monkeypatch
):
    """The import leaves a version's verdict when a later run publishes none."""
    from oddish.core import backfill_version_verdicts as backfill_module

    task, (v3, v4, v5), qa, artifact, _ = await _seed(session)
    experiment = await session.get(ExperimentModel, qa.experiment_id)
    agent = await session.get(TrialModel, artifact["trials"][0]["trial_id"])
    later = _qa(task, experiment, v4, [agent], [], with_verdict=False)
    later.created_at = qa.created_at + timedelta(hours=1)
    session.add(later)
    await session.flush()
    later_id = later.id

    async def read_artifact(trial, filename):
        # A classification-only run's qa_result.json carries a null verdict.
        return {**artifact, "verdict": None} if trial.id == later_id else artifact

    monkeypatch.setattr(backfill_module, "read_analysis_artifact", read_artifact)
    assert (await backfill_module._run_verdict(session, v4, later))[0] == (
        "classification_only"
    )

    outcome, payload, detail = await backfill_module.recover_version_verdict(
        session, v4
    )
    assert (outcome, detail) == ("recovered", qa.id)
    assert payload["verdict"] == "accept" and payload["_graded_by"] == qa.id


class AsyncMockArtifact:
    def __init__(self, value):
        self.value = value

    async def __call__(self, trial, filename):
        return self.value
