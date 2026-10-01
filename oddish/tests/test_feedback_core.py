"""Database tests for QA vote persistence and experiment membership."""

import os
import uuid

import pytest
from fastapi import HTTPException

from oddish.schemas import FeedbackCreate

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = pytest.mark.asyncio


async def _make_trial(session, *, run: str):
    from oddish.db.models import ExperimentModel, TaskModel, TrialModel

    experiment = ExperimentModel(name=f"exp-{run}")
    task = TaskModel(id=f"task-{run}", name=f"task-{run}", user="u", task_path="p")
    session.add_all([experiment, task])
    await session.flush()
    trial = TrialModel(
        id=f"trial-{run}",
        name=f"trial-{run}",
        task_id=task.id,
        experiment_id=experiment.id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
    )
    session.add(trial)
    await session.flush()
    return experiment, trial


async def test_qa_vote_persists_structured_subject():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_feedback_core
    from oddish.db import get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        row = await create_feedback_core(
            session,
            data=FeedbackCreate(
                target="qa_verdict",
                target_key="BAD_FAILURE",
                vote="disagree",
                trial_id=trial.id,
                body="The verifier evidence contradicts this.",
            ),
            experiment_id=experiment.id,
            org_id=None,
            user_id="user-1",
        )

        assert row.experiment_id == experiment.id
        assert row.trial_id == trial.id
        assert row.target == "qa_verdict"
        assert row.target_key == "BAD_FAILURE"
        assert row.vote == "disagree"
        assert row.created_by_user_id == "user-1"


async def test_foreign_experiment_cannot_accept_gathered_trial_feedback():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_feedback_core
    from oddish.db import experiment_trials, get_session, init_db
    from oddish.db.models import ExperimentModel

    await init_db()
    async with get_session() as session:
        _, trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        other = ExperimentModel(
            name=f"other-{uuid.uuid4().hex[:8]}", org_id="foreign-org"
        )
        session.add(other)
        await session.flush()
        await session.execute(
            experiment_trials.insert().values(
                experiment_id=other.id,
                trial_id=trial.id,
            )
        )

        with pytest.raises(HTTPException, match="trial not found") as excinfo:
            await create_feedback_core(
                session,
                data=FeedbackCreate(
                    target="qa_action_item",
                    target_key="item-1",
                    vote="agree",
                    trial_id=trial.id,
                ),
                experiment_id=other.id,
                org_id=None,
                user_id=None,
            )

        assert excinfo.value.status_code == 404


async def test_task_vote_anchors_to_the_trial_and_its_experiment():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_task_feedback_core
    from oddish.db import get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        row = await create_task_feedback_core(
            session,
            data=FeedbackCreate(
                target="qa_action_item",
                target_key="finding-abc",
                vote="disagree",
                trial_id=trial.id,
                body="This file is not part of the task.",
            ),
            task_id=trial.task_id,
            org_id=None,
            user_id="user-1",
        )
        assert row.trial_id == trial.id
        assert row.experiment_id == experiment.id
        assert row.target_key == "finding-abc"

        _, other_trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        with pytest.raises(HTTPException, match="trial not found") as excinfo:
            await create_task_feedback_core(
                session,
                data=FeedbackCreate(
                    target="qa_verdict",
                    target_key="GOOD_SUCCESS",
                    vote="agree",
                    trial_id=other_trial.id,
                ),
                task_id=trial.task_id,
                org_id=None,
                user_id=None,
            )
        assert excinfo.value.status_code == 404


async def test_report_votes_are_independent_and_keep_generating_run():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select
    from oddish.core.feedback import create_task_feedback_core
    from oddish.db import FeedbackModel, TrialModel, get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, subject = await _make_trial(session, run=uuid.uuid4().hex[:8])
        reports = []
        for kind in ("qa", "audit"):
            report = TrialModel(
                id=f"{subject.id}-{kind}",
                name=kind,
                kind=kind,
                task_id=subject.task_id,
                experiment_id=experiment.id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                harbor_config={"analysis_payload": {"trial_ids": [subject.id]}},
            )
            session.add(report)
            reports.append(report)
        await session.flush()
        qa, audit = reports
        ids = []
        for target, trial, report, key, vote in (
            ("qa_trajectory", subject, qa, "GOOD_FAILURE", "agree"),
            ("qa_task_verdict", qa, qa, "reject", "disagree"),
            ("qa_pre_trial", audit, audit, "report", "agree"),
        ):
            row = await create_task_feedback_core(
                session,
                task_id=subject.task_id,
                org_id=None,
                user_id="reviewer-1",
                data=FeedbackCreate(
                    target=target,
                    target_key=key,
                    vote=vote,
                    trial_id=trial.id,
                    report_trial_id=report.id,
                ),
            )
            ids.append(row.id)
        # Replacing the live analysis does not retarget prior feedback.
        subject.analysis = {"classification": "BAD_FAILURE", "_graded_by": "qa-new"}
        await session.flush()
        subject_id = subject.id
        session.expire_all()
        rows = list(
            (
                await session.scalars(
                    select(FeedbackModel).where(FeedbackModel.id.in_(ids))
                )
            ).all()
        )
        assert {row.target: row.vote for row in rows} == {
            "qa_trajectory": "agree",
            "qa_task_verdict": "disagree",
            "qa_pre_trial": "agree",
        }
        assert {row.created_by_user_id for row in rows} == {"reviewer-1"}
        assert {row.target: row.report_trial_id for row in rows} == {
            "qa_trajectory": f"{subject_id}-qa",
            "qa_task_verdict": f"{subject_id}-qa",
            "qa_pre_trial": f"{subject_id}-audit",
        }


@pytest.mark.parametrize("mismatch", ["org", "task", "kind", "evidence", "version"])
async def test_report_feedback_rejects_unrelated_producing_run(mismatch):
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_task_feedback_core
    from oddish.db import TrialModel, get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, subject = await _make_trial(session, run=uuid.uuid4().hex[:8])
        _, other = await _make_trial(session, run=uuid.uuid4().hex[:8])
        report = TrialModel(
            id=f"{subject.id}-qa",
            name="qa",
            kind="audit" if mismatch == "kind" else "qa",
            task_id=other.task_id if mismatch == "task" else subject.task_id,
            org_id="foreign" if mismatch == "org" else None,
            experiment_id=experiment.id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            harbor_config={
                "analysis_payload": {
                    "trial_ids": [] if mismatch == "evidence" else [subject.id]
                }
            },
        )
        session.add(report)
        await session.flush()
        if mismatch == "version":
            # Exercise the mismatch without needing a second version fixture.
            report.task_version_id = "different-version"
        with session.no_autoflush:
            with pytest.raises(HTTPException) as error:
                await create_task_feedback_core(
                    session,
                    task_id=subject.task_id,
                    org_id=None,
                    user_id="reviewer-1",
                    data=FeedbackCreate(
                        target="qa_trajectory",
                        target_key="GOOD_FAILURE",
                        vote="agree",
                        trial_id=subject.id,
                        report_trial_id=report.id,
                    ),
                )
        assert error.value.status_code == (
            422 if mismatch in {"kind", "evidence"} else 404
        )
        await session.rollback()


@pytest.mark.parametrize(
    "harbor_config",
    [None, {}, {"analysis_payload": None}, {"analysis_payload": {"trial_ids": None}}],
)
async def test_trajectory_vote_accepts_grader_named_by_trial_analysis(harbor_config):
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_task_feedback_core
    from oddish.db import TrialModel, get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, subject = await _make_trial(session, run=uuid.uuid4().hex[:8])
        report = TrialModel(
            id=f"{subject.id}-qa",
            name="qa",
            kind="qa",
            task_id=subject.task_id,
            experiment_id=experiment.id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            harbor_config=harbor_config,
        )
        session.add(report)
        subject.analysis = {"classification": "GOOD_FAILURE", "_graded_by": report.id}
        await session.flush()
        row = await create_task_feedback_core(
            session,
            task_id=subject.task_id,
            org_id=None,
            user_id="reviewer-1",
            data=FeedbackCreate(
                target="qa_trajectory",
                target_key="GOOD_FAILURE",
                vote="agree",
                trial_id=subject.id,
                report_trial_id=report.id,
            ),
        )
        assert row.report_trial_id == report.id
        await session.rollback()
