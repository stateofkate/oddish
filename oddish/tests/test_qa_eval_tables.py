"""The QA eval contract: stage specs, request checks, table constraints and
the two shared golden rules. Table tests need Postgres."""

from __future__ import annotations

import os
import uuid
from importlib import resources

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oddish.analyze.qa_stages import (
    STAGES,
    QAStage,
    prompt_sha256,
    shipped_prompt_sha256,
)
from oddish.core.qa_goldens import golden_state, original_id
from oddish.db import (
    ExperimentModel,
    QAGoldenCaseModel,
    QAPromptModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.schemas import QAEvalCreateRequest, QAGoldenCreate

needs_db = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
)


def test_the_shipped_prompt_hash_is_of_the_stripped_file():
    text = resources.files("oddish.analyze").joinpath("classify_prompt.txt").read_text()
    assert shipped_prompt_sha256(QAStage.POST_TRIAL) == prompt_sha256(text)
    assert prompt_sha256(f"  {text}\n\n") == prompt_sha256(text)


def test_only_post_trial_replays_are_graded():
    post = STAGES[QAStage.POST_TRIAL]
    assert post.replayable and not STAGES[QAStage.PRE_TRIAL].replayable
    assert post.grade.answer({"classification": "BAD_FAILURE"}) == "BAD_FAILURE"
    assert post.grade.answer({"classification": "maybe"}) is None
    assert post.grade.answer(None) is None
    assert post.grade.is_correct({"classification": "GOOD_SUCCESS"}, "GOOD_SUCCESS")


def _replay(**overrides) -> QAEvalCreateRequest:
    body = {"name": "run", "goldens": True, "prompt_sha256": "abc"}
    return QAEvalCreateRequest(**(body | overrides))


def test_a_replay_names_one_prompt_and_one_trial_set():
    assert _replay().stage == QAStage.POST_TRIAL
    assert _replay(
        goldens=False, source_trial_ids=[" t-1 ", "t-1"]
    ).source_trial_ids == ["t-1"]
    for bad, message in (
        ({"stage": "pre_trial"}, "pre_trial prompts cannot be replayed yet"),
        ({"prompt_text": "x", "prompt_name": "p"}, "exactly one of prompt_text"),
        ({"prompt_sha256": None}, "exactly one of prompt_text"),
        ({"prompt_sha256": None, "prompt_text": "x"}, "prompt_name is required"),
        ({"source_trial_ids": ["t-1"]}, "exactly one of goldens"),
        ({"goldens": False}, "exactly one of goldens"),
    ):
        with pytest.raises(ValidationError, match=message):
            _replay(**bad)


def test_a_golden_is_checked_against_its_stage():
    golden = QAGoldenCreate(
        stage="post_trial",
        source=" labels.csv ",
        source_trial_id="t-1",
        expected={"classification": "BAD_FAILURE", "extra": 1},
    )
    assert golden.expected == {"classification": "BAD_FAILURE"}
    assert golden.source == "labels.csv"
    finding = {"finding_id": "flag_semantics", "evidence_references": []}
    pre = QAGoldenCreate(
        stage="pre_trial",
        task_version_id="task-v1",
        expected={"task_judgment": "BAD", "required_findings": [finding]},
    )
    assert pre.expected["required_findings"] == [finding]
    for bad, message in (
        ({"expected": {"classification": "FINE"}}, "classification must be one of"),
        ({"task_version_id": "task-v1"}, "need source_trial_id alone"),
        ({"source_trial_id": None}, "need source_trial_id alone"),
        (
            {"stage": "pre_trial", "expected": {"required_findings": ["flag"]}},
            "objects with a finding_id",
        ),
        (
            {"stage": "pre_trial", "expected": {"required_findings": [finding]}},
            "need task_version_id alone",
        ),
    ):
        body = {
            "stage": "post_trial",
            "source_trial_id": "t-1",
            "expected": {"classification": "BAD_FAILURE"},
        }
        with pytest.raises(ValidationError, match=message):
            QAGoldenCreate(**(body | bad))


async def _seed(session, org: str) -> dict[str, str]:
    """One task with version v1 and three finished solver trials: a plain
    one, a copy of it, and a copy whose original is not in this org."""
    run = uuid.uuid4().hex[:8]
    task_id = f"golden-{run}"
    experiment = ExperimentModel(name=f"exp-{run}", org_id=org)
    session.add(experiment)
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org,
            user="u",
            task_path="p",
            status=TaskStatus.COMPLETED,
        )
    )
    await session.flush()
    session.add(
        TaskVersionModel(
            id=f"{task_id}-v1",
            task_id=task_id,
            version=1,
            task_path="p",
            content_hash="hash-at-labeling",
        )
    )
    await session.flush()
    ids = {
        "task": task_id,
        "version": f"{task_id}-v1",
        "trial": f"{task_id}-1",
        "copy": f"{task_id}-2",
        "stray": f"{task_id}-3",
    }
    copied_from = {"copy": ids["trial"], "stray": "prod-trial-elsewhere"}
    for key in ("trial", "copy", "stray"):
        session.add(
            TrialModel(
                id=ids[key],
                name=ids[key],
                task_id=task_id,
                task_version_id=ids["version"],
                experiment_id=experiment.id,
                org_id=org,
                agent="codex",
                provider="local",
                queue_key="q",
                kind="agent",
                status=TrialStatus.FAILED,
                harbor_config=(
                    {"imported_source": {"trial_id": copied_from[key]}}
                    if key in copied_from
                    else None
                ),
            )
        )
    await session.flush()
    return ids


def _golden(org: str, version: str, **overrides) -> QAGoldenCaseModel:
    values = {
        "id": uuid.uuid4().hex[:8],
        "org_id": org,
        "stage": "post_trial",
        "source_trial_id": f"trial-{uuid.uuid4().hex[:8]}",
        "task_version_id": version,
        "task_version_content_hash": "hash-at-labeling",
        "expected": {"classification": "BAD_FAILURE"},
    }
    return QAGoldenCaseModel(**(values | overrides))


@needs_db
@pytest.mark.asyncio
async def test_one_live_golden_per_subject_whatever_its_source(session):
    org = f"org-{uuid.uuid4().hex[:8]}"
    first = _golden(org, "v", source="labels.csv")
    session.add(first)
    await session.flush()
    for source in ("labels.csv", "other.csv", None):
        async with session.begin_nested():
            session.add(
                _golden(org, "v", source=source, source_trial_id=first.source_trial_id)
            )
            with pytest.raises(IntegrityError, match="uq_qa_golden_cases_live_subject"):
                await session.flush()

    # Another org, or a retired golden, frees the subject.
    session.add(_golden("other-org", "v", source_trial_id=first.source_trial_id))
    first.deleted_at = utcnow()
    session.add(_golden(org, "v", source_trial_id=first.source_trial_id))
    await session.flush()


@needs_db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "source_trial_id"),
    [("post_trial", None), ("pre_trial", "t-1"), ("mid_trial", "t-1")],
)
async def test_the_subject_must_match_the_stage(session, stage, source_trial_id):
    session.add(_golden("org", "v", stage=stage, source_trial_id=source_trial_id))
    with pytest.raises(IntegrityError, match="ck_qa_golden_cases_"):
        await session.flush()


@needs_db
@pytest.mark.asyncio
async def test_a_prompt_is_stored_once_per_org_stage_and_hash(session):
    sha = uuid.uuid4().hex

    def prompt(org_id: str | None, stage: str = "post_trial") -> QAPromptModel:
        return QAPromptModel(
            org_id=org_id, stage=stage, sha256=sha, text="t", source="replay"
        )

    session.add_all([prompt(None), prompt("org-a"), prompt(None, "pre_trial")])
    await session.flush()
    for duplicate in (prompt(None), prompt("org-a")):
        async with session.begin_nested():
            session.add(duplicate)
            with pytest.raises(IntegrityError, match="uq_qa_prompts_org_stage_sha256"):
                await session.flush()
    async with session.begin_nested():
        session.add(
            QAPromptModel(stage="post_trial", sha256=sha, text="t", source="git")
        )
        with pytest.raises(IntegrityError, match="ck_qa_prompts_source"):
            await session.flush()


@needs_db
@pytest.mark.asyncio
async def test_golden_state_is_counted_waiting_or_stale(session):
    org = f"org-{uuid.uuid4().hex[:8]}"
    ids = await _seed(session, org)
    version = ids["version"]
    goldens = {
        "waiting": _golden(org, version),
        "counted": _golden(org, version, reviewed_at=utcnow()),
        # Labeled before content hashes existed: nothing to compare.
        "unpinned": _golden(org, version, task_version_content_hash=None),
        "changed": _golden(org, version, task_version_content_hash="old-bytes"),
        "changed_reviewed": _golden(
            org, version, task_version_content_hash="old-bytes", reviewed_at=utcnow()
        ),
        "version_gone": _golden(org, f"{version}-deleted", reviewed_at=utcnow()),
    }
    session.add_all(goldens.values())
    await session.flush()

    rows = dict(
        (
            await session.execute(
                select(QAGoldenCaseModel.id, golden_state()).where(
                    QAGoldenCaseModel.org_id == org
                )
            )
        ).all()
    )
    assert {name: rows[g.id] for name, g in goldens.items()} == {
        "waiting": "waiting",
        "counted": "counted",
        "unpinned": "waiting",
        "changed": "stale",
        "changed_reviewed": "stale",
        "version_gone": "stale",
    }
    counted = await session.scalars(
        select(QAGoldenCaseModel.id).where(
            QAGoldenCaseModel.org_id == org, golden_state() == "counted"
        )
    )
    assert list(counted) == [goldens["counted"].id]


@needs_db
@pytest.mark.asyncio
async def test_a_copy_is_keyed_on_its_original_only_when_the_original_is_here(
    session,
):
    org = f"org-{uuid.uuid4().hex[:8]}"
    ids = await _seed(session, org)

    async def originals() -> dict[str, str]:
        rows = await session.execute(
            select(TrialModel.id, original_id(TrialModel)).where(
                TrialModel.task_id == ids["task"]
            )
        )
        return dict(rows.all())

    assert await originals() == {
        ids["trial"]: ids["trial"],
        ids["copy"]: ids["trial"],
        # Its original is not in this database.
        ids["stray"]: ids["stray"],
    }

    # A deleted original no longer exists, so the copy stands for itself.
    original = await session.get(TrialModel, ids["trial"])
    original.deleted_at = utcnow()
    await session.flush()
    assert await originals() == {ids["copy"]: ids["copy"], ids["stray"]: ids["stray"]}
