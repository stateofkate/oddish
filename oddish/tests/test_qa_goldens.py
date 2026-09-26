"""Adding, labeling, reviewing and retiring goldens, and the labeling queue.
Needs Postgres."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.analyze.qa_stages import QAStage
from oddish.core.qa_goldens import (
    add_golden,
    label_queue,
    label_trial,
    list_goldens,
    retire_golden,
    review_golden,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    utcnow,
)
from oddish.schemas import QAGoldenCreate, QAGoldenLabel

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
    ),
]


@dataclass
class Seed:
    session: object
    org: str
    task: str
    version: str
    experiment: ExperimentModel

    async def trial(self, suffix: str, **overrides) -> str:
        values = {
            "id": f"{self.task}-{suffix}",
            "name": f"{self.task}-{suffix}",
            "task_id": self.task,
            "task_version_id": self.version,
            "experiment_id": self.experiment.id,
            "org_id": self.org,
            "agent": "codex",
            "provider": "local",
            "queue_key": "q",
            "kind": "agent",
            "status": TrialStatus.FAILED,
            "reward": 0.0,
        }
        self.session.add(TrialModel(**(values | overrides)))
        await self.session.flush()
        return values["id"]

    def copy_of(self, trial_id: str) -> dict:
        return {"harbor_config": {"imported_source": {"trial_id": trial_id}}}

    async def label(self, trial_id: str, classification: str, note: str | None = None):
        return await label_trial(
            self.session,
            trial_id=trial_id,
            data=QAGoldenLabel(classification=classification, note=note),
            org_id=self.org,
            user_id="ana",
        )

    async def add(self, trial_id: str, classification: str = "BAD_FAILURE"):
        return await add_golden(
            self.session,
            data=QAGoldenCreate(
                stage=QAStage.POST_TRIAL,
                source="labels.csv",
                source_trial_id=trial_id,
                expected={"classification": classification},
            ),
            org_id=self.org,
            user_id="importer",
        )

    async def list(self, **filters):
        return await list_goldens(self.session, org_id=self.org, **filters)

    async def change_task(self, version: str | None = None) -> None:
        row = await self.session.get(TaskVersionModel, version or self.version)
        row.content_hash = f"changed-{uuid.uuid4().hex[:6]}"
        await self.session.flush()


@pytest_asyncio.fixture
async def seed(session) -> Seed:
    run = uuid.uuid4().hex[:8]
    org, task = f"org-{run}", f"golden-{run}"
    experiment = ExperimentModel(name=f"exp-{run}", org_id=org)
    session.add(experiment)
    session.add(
        TaskModel(
            id=task,
            name=task,
            org_id=org,
            user="u",
            task_path="p",
            status=TaskStatus.COMPLETED,
        )
    )
    await session.flush()
    session.add(
        TaskVersionModel(
            id=f"{task}-v1",
            task_id=task,
            version=1,
            task_path="p",
            content_hash="hash-at-labeling",
        )
    )
    await session.flush()
    return Seed(session, org, task, f"{task}-v1", experiment)


async def _status(call) -> int:
    with pytest.raises(HTTPException) as caught:
        await call
    return caught.value.status_code


async def test_an_added_golden_waits_pinned_to_its_trials_version(seed):
    trial = await seed.trial("1")
    golden = await seed.add(trial)
    assert (golden.task_version_id, golden.source, golden.state) == (
        seed.version,
        "labels.csv",
        "waiting",
    )
    assert [g.id for g in await seed.list(state="waiting")] == [golden.id]
    assert await seed.list(state="counted") == []
    assert await seed.list(source="other.csv") == []
    assert await list_goldens(seed.session, org_id="another-org") == []

    assert await _status(seed.add(trial, "GOOD_FAILURE")) == 409
    await retire_golden(seed.session, golden_id=golden.id, org_id=seed.org)
    corrected = await seed.add(trial, "GOOD_FAILURE")
    assert [g.id for g in await seed.list()] == [corrected.id]


async def test_goldens_are_refused_on_other_orgs_and_non_solver_trials(seed):
    qa = await seed.trial("qa", kind="qa", agent="claude-code")
    oracle = await seed.trial("oracle", agent="oracle")
    running = await seed.trial("running", status=TrialStatus.RUNNING)
    assert await _status(seed.add(qa)) == 422
    assert await _status(seed.add(oracle)) == 422
    # Unfinished: no run could replay it.
    assert await _status(seed.label(running, "BAD_FAILURE")) == 422
    assert await _status(seed.add("missing")) == 404
    assert await _status(seed.label(oracle, "BAD_FAILURE")) == 422

    trial = await seed.trial("1")
    golden = await seed.add(trial)
    theirs = Seed(seed.session, "another-org", seed.task, seed.version, seed.experiment)
    assert await _status(theirs.add(trial)) == 404
    assert await _status(theirs.label(trial, "BAD_FAILURE")) == 404
    for call in (
        review_golden(seed.session, golden_id=golden.id, org_id="x", user_id="u"),
        retire_golden(seed.session, golden_id=golden.id, org_id="x"),
    ):
        assert await _status(call) == 404


async def test_a_pre_trial_golden_labels_a_version_of_the_org(seed):
    data = QAGoldenCreate(
        stage=QAStage.PRE_TRIAL,
        task_version_id=seed.version,
        expected={"required_findings": [{"finding_id": "flag_semantics"}]},
    )
    golden = await add_golden(seed.session, data=data, org_id=seed.org, user_id="u")
    assert (golden.source_trial_id, golden.task_version_id) == (None, seed.version)
    assert [g.id for g in await seed.list(stage=QAStage.PRE_TRIAL)] == [golden.id]
    assert await seed.list(stage=QAStage.POST_TRIAL) == []
    assert (
        await _status(add_golden(seed.session, data=data, org_id="x", user_id="u"))
        == 404
    )


async def test_a_copy_is_labeled_and_found_as_its_original(seed):
    original = await seed.trial("1")
    copy = await seed.trial("copy", **seed.copy_of(original))
    stray = await seed.trial("stray", **seed.copy_of("prod-trial-elsewhere"))

    golden = await seed.label(copy, "BAD_FAILURE")
    assert golden.source_trial_id == original
    for trial in (copy, original):
        assert [g.id for g in await seed.list(trial_ids=[trial])] == [golden.id]
    assert await _status(seed.add(original)) == 409
    # A copy whose original is not here stands for itself.
    assert (await seed.add(stray)).source_trial_id == stray


async def test_relabeling_confirms_the_same_label_and_replaces_a_new_one(seed):
    trial = await seed.trial("1")
    imported = await seed.add(trial)

    first = await seed.label(trial, "BAD_FAILURE", note="reads the answer file")
    assert (first.id, first.state, first.source) == (
        imported.id,
        "counted",
        "labels.csv",
    )
    assert first.note == "reads the answer file"
    # No note keeps the note; an empty one clears it.
    assert (await seed.label(trial, "BAD_FAILURE")).note == "reads the answer file"
    assert (await seed.label(trial, "BAD_FAILURE", note="")).note == ""

    changed = await seed.label(trial, "GOOD_FAILURE")
    assert (changed.id != first.id, changed.source, changed.note) == (True, None, "")
    assert [(g.id, g.expected, g.state) for g in await seed.list()] == [
        (changed.id, {"classification": "GOOD_FAILURE"}, "counted")
    ]


async def test_review_and_a_same_label_put_both_clear_stale(seed):
    reviewed = await seed.add(await seed.trial("1"))
    relabeled = await seed.label(await seed.trial("2"), "BAD_FAILURE")
    await review_golden(
        seed.session, golden_id=reviewed.id, org_id=seed.org, user_id="ana"
    )
    await seed.change_task()
    assert {g.state for g in await seed.list()} == {"stale"}
    # The state filter is the same rule: stale goldens are not waiting.
    assert await seed.list(state="waiting") == []
    assert len(await seed.list(state="stale")) == 2

    again = await review_golden(
        seed.session, golden_id=reviewed.id, org_id=seed.org, user_id="ben"
    )
    assert again.state == "counted"
    assert (await seed.label(f"{seed.task}-2", "BAD_FAILURE")).state == "counted"
    [row] = await seed.list(trial_ids=[f"{seed.task}-2"])
    assert (row.id, row.state) == (relabeled.id, "counted")


async def test_an_experiments_queue_puts_failures_first(seed):
    passed = await seed.trial(
        "a-passed",
        status=TrialStatus.SUCCESS,
        reward=1.0,
        analysis={"classification": "GOOD_SUCCESS"},
    )
    failed = await seed.trial(
        "b-failed",
        analysis={
            "classification": "BAD_FAILURE",
            "subtype": "Reference leak",
            "root_cause": "The solver read the reference binary.",
        },
    )
    copy = await seed.trial("c-copy", **seed.copy_of(passed))
    await seed.trial("oracle", agent="oracle")
    await seed.trial("qa", kind="qa")
    await seed.trial("running", status=TrialStatus.RUNNING, reward=None)
    await seed.trial("retried", superseded_by_trial_id=failed)
    golden = await seed.label(copy, "GOOD_SUCCESS")
    waiting = await seed.add(failed)

    for ref in (seed.experiment.id, seed.experiment.name):
        queue = await label_queue(seed.session, org_id=seed.org, experiment=ref)
        assert queue.title == seed.experiment.name
        assert [
            (t.trial_id, t.group, t.golden and (t.golden.id, t.golden.state))
            for t in queue.trials
        ] == [
            (failed, "failed", (waiting.id, "waiting")),
            (copy, "labeled", (golden.id, "counted")),
            (passed, "labeled", (golden.id, "counted")),
        ]
        assert (queue.trials[0].qa_subtype, queue.trials[0].qa_reason) == (
            "Reference leak",
            "The solver read the reference binary.",
        )
        assert queue.trials[2].qa_classification == "GOOD_SUCCESS"

    assert (
        await _status(
            label_queue(seed.session, org_id="x", experiment=seed.experiment.id)
        )
        == 404
    )
    for kwargs in ({}, {"experiment": "x", "review": "waiting"}):
        assert (
            await _status(label_queue(seed.session, org_id=seed.org, **kwargs)) == 422
        )


async def test_a_collections_gathered_trials_are_queued(seed):
    trial = await seed.trial("1")
    collection = ExperimentModel(
        name=f"collection-{seed.org}", org_id=seed.org, is_collection=True
    )
    seed.session.add(collection)
    await seed.session.flush()
    await seed.session.execute(
        experiment_trials.insert().values(experiment_id=collection.id, trial_id=trial)
    )
    queue = await label_queue(seed.session, org_id=seed.org, experiment=collection.id)
    assert [t.trial_id for t in queue.trials] == [trial]


async def test_review_queues_hold_the_goldens_that_do_not_count(seed):
    second = f"{seed.version}-2"
    seed.session.add(
        TaskVersionModel(
            id=second,
            task_id=seed.task,
            version=2,
            task_path="p",
            content_hash="second",
        )
    )
    await seed.session.flush()
    waiting = await seed.add(await seed.trial("waiting"))
    await seed.label(await seed.trial("counted", reward=1.0), "GOOD_SUCCESS")
    stale = await seed.label(
        await seed.trial("stale", task_version_id=second), "BAD_FAILURE"
    )
    await seed.change_task(second)
    # A copy of a golden's trial is not queued a second time.
    await seed.trial("copy", **seed.copy_of(f"{seed.task}-waiting"))
    # A golden whose trial was deleted has nothing left to label.
    gone = await seed.add(await seed.trial("deleted"))
    (await seed.session.get(TrialModel, gone.source_trial_id)).deleted_at = utcnow()
    await seed.session.flush()

    queued = await label_queue(seed.session, org_id=seed.org, review="waiting")
    assert queued.title == "Waiting for review"
    assert [(t.trial_id, t.golden.id) for t in queued.trials] == [
        (f"{seed.task}-waiting", waiting.id)
    ]
    queued = await label_queue(seed.session, org_id=seed.org, review="stale")
    assert [(t.trial_id, t.golden.id, t.group) for t in queued.trials] == [
        (f"{seed.task}-stale", stale.id, "failed")
    ]
    other = await label_queue(seed.session, org_id="another-org", review="waiting")
    assert other.trials == []
