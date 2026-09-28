"""Scoring rules behind the QA Evals page. DB tests need Postgres.

The seeded org has one task version and three solver trials with goldens:
trial 1 BAD_FAILURE and trial 2 GOOD_FAILURE reviewed, trial 3 BAD_SUCCESS
waiting for review. Two runs replay all three trials:

- shipped prompt:  1 right, 2 wrong, 3 off (no counted golden)
- candidate prompt: 1 invalid output, 2 errored, 3 still running
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from oddish.analyze.qa_stages import (
    QAStage,
    prompt_sha256,
    shipped_prompt,
    shipped_prompt_sha256,
)
from oddish.core.qa_eval_scores import (
    _frontier,
    _models,
    _model_name,
    _score_runs,
    get_prompt_version_text_core,
    get_qa_eval_run_core,
    qa_eval_status_core,
)
from oddish.db import (
    AnalysisStatus,
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
from oddish.schemas import QAEvalRunSummary

needs_db = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
)

POST = QAStage.POST_TRIAL
SHIPPED = shipped_prompt_sha256(POST)
CANDIDATE = "Classify with the causal test first."
OK, BAD, RUNNING = TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.RUNNING
GOOD, FAILED = AnalysisStatus.SUCCESS, AnalysisStatus.FAILED


@pytest.mark.parametrize("with_golden", [True, False])
@pytest.mark.parametrize("preflight", [True, False])
@pytest.mark.parametrize(
    "status,analysis_status,analysis,valid",
    [
        (OK, GOOD, None, False),
        (OK, GOOD, {}, False),
        (OK, GOOD, {"classification": "maybe"}, False),
        (OK, GOOD, {"classification": "BAD_FAILURE"}, True),
        (OK, GOOD, {"classification": "GOOD_FAILURE"}, True),
        (RUNNING, None, None, False),
        (BAD, GOOD, {"classification": "BAD_FAILURE"}, False),
        (OK, FAILED, {"classification": "BAD_FAILURE"}, False),
    ],
)
def test_preflight_requires_a_usable_answer(
    with_golden, preflight, status, analysis_status, analysis, valid
):
    replay = SimpleNamespace(
        id="replay",
        task_id="task",
        task_version_id="version",
        source_trial_id="source",
        original_trial_id="source",
        experiment_id="run",
        experiment_name="check",
        created_at=utcnow(),
        model="anthropic/claude-sonnet-5",
        agent="claude-code",
        reasoning_effort=None,
        configuration_sha256=None,
        golden_set_sha256=None,
        prompt_sha256=SHIPPED,
        prompt_name="",
        subset=True,
        preflight=preflight,
        status=status,
        analysis_status=analysis_status,
        analysis=analysis,
        cost_usd=None,
        error=None,
    )
    goldens = (
        {
            "source": QAGoldenCaseModel(
                id="golden", expected={"classification": "BAD_FAILURE"}
            )
        }
        if with_golden
        else {}
    )

    [run] = _score_runs([replay], POST, goldens, {})

    # A valid but incorrect answer still passes the execution check. Removing
    # a golden affects accuracy, not whether the model produced usable output.
    assert run.summary.preflight_passed is (preflight and valid)


def _trial(ids: dict, trial_id: str, **values) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=ids["task"],
        task_version_id=ids["version"],
        org_id=ids["org"],
        provider="local",
        queue_key="q",
        **values,
    )


def _replay(ids, run, source, sha, status, analysis_status, got, cost, **extra):
    replay_id = f"{ids['task']}-r{uuid.uuid4().hex[:6]}"
    payload = {"trial_ids": [source], "prompt_sha256": sha, "prompt_name": "sent"}
    return _trial(
        ids,
        replay_id,
        experiment_id=run,
        agent="claude-code",
        model=extra.pop("model", "bedrock/claude-sonnet-5"),
        kind="qa_eval",
        status=status,
        analysis_status=analysis_status,
        analysis={"classification": got} if got else None,
        analysis_error="bad json" if analysis_status == FAILED else None,
        error_message=(
            "No reward file at /tmp/jobs/{0}/verifier/reward.txt\ntrace".format(
                replay_id
            )
            if status == BAD
            else None
        ),
        cost_usd=cost,
        harbor_config={"analysis_payload": payload | extra},
    )


async def _run(session, ids, name: str, minutes: int, owner: str = "ana") -> str:
    run = ExperimentModel(
        name=name,
        org_id=ids["org"],
        owner_user_id=owner,
        created_at=ids["now"] + timedelta(minutes=minutes),
    )
    session.add(run)
    await session.flush()
    return run.id


async def _seed(session) -> dict:
    tag = uuid.uuid4().hex[:8]
    ids: dict = {"org": f"org-{tag}", "task": f"scores-{tag}", "now": utcnow()}
    ids["version"] = f"{ids['task']}-v1"
    ids["trials"] = [f"{ids['task']}-{n}" for n in (1, 2, 3)]
    session.add(
        TaskModel(
            id=ids["task"],
            name=ids["task"],
            org_id=ids["org"],
            user="u",
            task_path="p",
            status=TaskStatus.COMPLETED,
        )
    )
    solvers = await _run(session, ids, "solvers", -60)
    session.add(
        TaskVersionModel(
            id=ids["version"],
            task_id=ids["task"],
            version=1,
            task_path="p",
            content_hash="h1",
        )
    )
    await session.flush()
    for trial_id, label, reviewed in zip(
        ids["trials"], ("BAD_FAILURE", "GOOD_FAILURE", "BAD_SUCCESS"), (1, 1, 0)
    ):
        session.add(
            _trial(
                ids,
                trial_id,
                experiment_id=solvers,
                agent="codex",
                kind="agent",
                status=OK,
            )
        )
        session.add(
            QAGoldenCaseModel(
                org_id=ids["org"],
                stage=POST.value,
                source_trial_id=trial_id,
                task_version_id=ids["version"],
                task_version_content_hash="h1",
                expected={"classification": label},
                reviewed_at=ids["now"] if reviewed else None,
            )
        )
    ids["shipped"] = await _run(session, ids, "shipped replay", 0)
    ids["candidate"] = await _run(session, ids, "candidate replay", 5, owner="ben")
    ids["candidate_sha"] = prompt_sha256(CANDIDATE)
    t1, t2, t3 = ids["trials"]
    shipped, candidate, sha = ids["shipped"], ids["candidate"], ids["candidate_sha"]
    session.add_all(
        [
            _replay(ids, shipped, t1, SHIPPED, OK, GOOD, "BAD_FAILURE", 1.0),
            _replay(ids, shipped, t2, SHIPPED, OK, GOOD, "BAD_FAILURE", 1.0),
            _replay(ids, shipped, t3, SHIPPED, OK, GOOD, "BAD_SUCCESS", None),
            _replay(ids, candidate, t1, sha, OK, FAILED, None, 0.5),
            _replay(ids, candidate, t2, sha, BAD, None, None, None),
            _replay(ids, candidate, t3, sha, RUNNING, None, None, None),
            QAPromptModel(
                org_id=ids["org"],
                stage=POST.value,
                sha256=sha,
                text=CANDIDATE,
                name="causal-test",
                source="replay",
                created_by_user_id="ben",
                created_at=ids["now"] + timedelta(minutes=4),
            ),
        ]
    )
    await session.flush()
    return ids


async def _status(session, ids):
    return await qa_eval_status_core(session, org_id=ids["org"], stage=POST)


async def _finish(session, ids) -> None:
    """The candidate's running replay finishes (its golden does not count,
    so it scores nothing), which lets the run be compared."""
    running = await session.scalar(
        select(TrialModel).where(
            TrialModel.experiment_id == ids["candidate"],
            TrialModel.status == RUNNING,
        )
    )
    running.status, running.analysis_status = OK, GOOD
    running.analysis = {"classification": "BAD_SUCCESS"}
    await session.flush()


@needs_db
@pytest.mark.asyncio
async def test_status_scores_each_run_on_the_counted_goldens(session):
    ids = await _seed(session)
    status = await _status(session, ids)

    assert status.replayable
    assert status.production_prompt_sha256 == SHIPPED
    goldens = status.goldens
    assert (goldens.counted, goldens.waiting, goldens.stale) == (2, 1, 0)
    assert [(c.label, c.count) for c in goldens.labels] == [
        ("BAD_FAILURE", 1),
        ("GOOD_FAILURE", 1),
    ]

    shipped, candidate = status.runs
    assert shipped.title == f"production {SHIPPED[:8]}"
    assert (shipped.replays, shipped.with_golden, shipped.judged) == (3, 2, 2)
    assert (shipped.correct, shipped.accuracy, shipped.charted) == (1, 0.5, True)
    assert (shipped.cost_usd, shipped.cost_per_trial, shipped.cost_missing) == (
        2.0,
        1.0,
        1,
    )
    # Invalid output is judged (wrong); errored and running are not. The
    # running replay is pending although its golden does not count.
    assert candidate.title == "causal-test"
    assert (candidate.judged, candidate.errored, candidate.pending) == (1, 1, 1)
    assert (candidate.correct, candidate.accuracy) == (0, 0.0)
    # The running replay has no cost yet; the errored one finished without.
    assert (candidate.cost_per_trial, candidate.cost_missing) == (0.5, 1)
    assert shipped.model_name == "claude-sonnet-5"

    assert status.production_run_id == ids["shipped"]
    # A run still replaying is scored but not compared.
    assert candidate.charted is False
    assert status.frontier == [ids["shipped"]]
    assert [(m.runs, m.latest_run_id) for m in status.models] == [(1, ids["shipped"])]
    assert status.cost_per_trial == 2.5 / 3
    spend = status.spend
    assert (spend.total_usd, spend.trials, spend.cost_missing) == (2.5, 6, 2)
    assert [(m.model_name, m.cost_usd) for m in spend.by_model] == [
        ("claude-sonnet-5", 2.5)
    ]

    await _finish(session, ids)
    status = await _status(session, ids)
    candidate = status.runs[-1]
    assert (candidate.pending, candidate.charted) == (0, True)
    # The candidate is cheaper and the shipped prompt more accurate.
    assert status.frontier == [ids["candidate"], ids["shipped"]]
    [model] = status.models
    assert (model.model_name, model.runs, model.latest_run_id) == (
        "claude-sonnet-5",
        2,
        ids["candidate"],
    )
    assert status.spend.cost_missing == 3


@needs_db
@pytest.mark.asyncio
async def test_each_golden_is_in_one_count_and_only_if_its_trial_exists(session):
    ids = await _seed(session)
    t1, _, t3 = ids["trials"]
    # Trial 3's waiting golden was labeled against older bytes: stale.
    golden = await session.scalar(
        select(QAGoldenCaseModel).where(QAGoldenCaseModel.source_trial_id == t3)
    )
    golden.task_version_content_hash = "older-bytes"
    await session.flush()
    goldens = (await _status(session, ids)).goldens
    assert (goldens.counted, goldens.waiting, goldens.stale) == (2, 0, 1)

    # A golden whose trial was deleted is in no count and scores nothing.
    trial = await session.get(TrialModel, t1)
    trial.deleted_at = utcnow()
    await session.flush()
    status = await _status(session, ids)
    assert (status.goldens.counted, status.goldens.stale) == (1, 1)
    assert [c.count for c in status.goldens.labels] == [1]
    assert status.runs[0].with_golden == 1


@needs_db
@pytest.mark.asyncio
async def test_a_changed_task_version_drops_every_golden_out_of_the_scores(session):
    ids = await _seed(session)
    version = await session.get(TaskVersionModel, ids["version"])
    version.content_hash = "replaced-in-place"
    await session.flush()
    status = await _status(session, ids)
    goldens = status.goldens
    assert (goldens.counted, goldens.waiting, goldens.stale) == (0, 0, 3)
    # Both runs are still listed (their cost counts), but none scores.
    assert [(r.with_golden, r.accuracy, r.charted) for r in status.runs] == [
        (0, None, False),
        (0, None, False),
    ]
    assert (status.production_run_id, status.frontier, status.models) == (None, [], [])


@needs_db
@pytest.mark.asyncio
async def test_a_run_lists_every_case_in_one_group(session):
    ids = await _seed(session)
    t1, t2, t3 = ids["trials"]
    shipped = await get_qa_eval_run_core(
        session, org_id=ids["org"], experiment_id=ids["shipped"]
    )
    assert sorted(
        (c.source_trial_id, c.expected, c.got, c.outcome, c.group)
        for c in shipped.cases
    ) == [
        (t1, "BAD_FAILURE", "BAD_FAILURE", "right", "right"),
        (t2, "GOOD_FAILURE", "BAD_FAILURE", "wrong", "wrong"),
        (t3, None, "BAD_SUCCESS", "off", "no_golden"),
    ]
    assert shipped.summary.experiment_id == ids["shipped"]
    assert shipped.error_groups == []

    candidate = await get_qa_eval_run_core(
        session, org_id=ids["org"], experiment_id=ids["candidate"]
    )
    by_trial = {c.source_trial_id: c for c in candidate.cases}
    assert [
        (by_trial[t].outcome, by_trial[t].group, by_trial[t].error)
        for t in (t1, t2, t3)
    ] == [
        ("invalid", "wrong", "bad json"),
        ("errored", "not_judged", by_trial[t2].error),
        ("off", "no_golden", None),
    ]
    assert by_trial[t2].error.startswith("No reward file at /tmp/jobs/")
    # Errored replays grouped by first line, the per-trial path masked.
    assert [(g.message, g.count) for g in candidate.error_groups] == [
        ("No reward file at <path>", 1)
    ]

    with pytest.raises(HTTPException) as caught:
        await get_qa_eval_run_core(
            session, org_id="other-org", experiment_id=ids["shipped"]
        )
    assert caught.value.status_code == 404


@needs_db
@pytest.mark.asyncio
async def test_a_subset_run_is_scored_but_left_out_of_the_comparisons(session):
    ids = await _seed(session)
    await _finish(session, ids)
    rerun = await _run(session, ids, "rerun of one trial", 10)
    # The newest, cheapest and most accurate run of the shipped prompt: it
    # would lead every comparison if its one trial stood for the goldens.
    session.add(
        _replay(
            ids,
            rerun,
            ids["trials"][0],
            SHIPPED,
            OK,
            GOOD,
            "BAD_FAILURE",
            0.1,
            subset=True,
        )
    )
    await session.flush()
    status = await _status(session, ids)
    subset = status.runs[-1]
    assert (subset.subset, subset.accuracy, subset.charted) == (True, 1.0, False)
    assert status.production_run_id == ids["shipped"]
    assert status.frontier == [ids["candidate"], ids["shipped"]]
    assert [(m.runs, m.latest_run_id) for m in status.models] == [(2, ids["candidate"])]
    # Its spend still counts toward what a trial costs.
    assert status.cost_per_trial == 2.6 / 4
    [shipped] = [v for v in status.versions if v.sha256 == SHIPPED]
    assert (shipped.runs, shipped.latest_run_id) == (2, ids["shipped"])


@needs_db
@pytest.mark.asyncio
async def test_a_copied_trial_scores_as_the_original_when_it_is_here(session):
    ids = await _seed(session)
    t1 = ids["trials"][0]
    copies = await _run(session, ids, "copies", -30)
    copy, stray = f"{t1}-copy", f"{ids['task']}-stray"
    for trial_id, original in ((copy, t1), (stray, "prod-trial-elsewhere")):
        session.add(
            _trial(
                ids,
                trial_id,
                experiment_id=copies,
                agent="codex",
                kind="agent",
                status=OK,
                harbor_config={"imported_source": {"trial_id": original}},
            )
        )
    # The stray copy's original is not here, so its label is its own.
    session.add(
        QAGoldenCaseModel(
            org_id=ids["org"],
            stage=POST.value,
            source_trial_id=stray,
            task_version_id=ids["version"],
            expected={"classification": "GOOD_FAILURE"},
            reviewed_at=ids["now"],
        )
    )
    rerun = await _run(session, ids, "replay over copies", 10)
    session.add_all(
        [
            _replay(ids, rerun, copy, SHIPPED, OK, GOOD, "BAD_FAILURE", 1.0),
            _replay(ids, rerun, stray, SHIPPED, OK, GOOD, "GOOD_FAILURE", 1.0),
        ]
    )
    await session.flush()

    run = await get_qa_eval_run_core(session, org_id=ids["org"], experiment_id=rerun)
    by_trial = {c.source_trial_id: c for c in run.cases}
    assert (by_trial[copy].original_trial_id, by_trial[copy].outcome) == (
        t1,
        "right",
    )
    assert (by_trial[stray].original_trial_id, by_trial[stray].outcome) == (
        stray,
        "right",
    )
    assert run.summary.accuracy == 1.0


@needs_db
@pytest.mark.asyncio
async def test_versions_list_the_shipped_recorded_and_run_only_prompts(session):
    ids = await _seed(session)
    later = "Classify, then check the verifier."
    unrecorded = await _run(session, ids, "unrecorded prompt", 10, owner="cy")
    session.add_all(
        [
            _replay(
                ids, unrecorded, ids["trials"][0], "f" * 64, OK, GOOD, "BAD_FAILURE", 1
            ),
            QAPromptModel(
                org_id=ids["org"],
                stage=POST.value,
                sha256=prompt_sha256(later),
                text=later,
                name="later",
                source="replay",
                created_at=ids["now"] + timedelta(minutes=20),
            ),
        ]
    )
    await session.flush()
    versions = (await _status(session, ids)).versions
    assert [
        (v.sha256, v.name, v.author, v.text_available, v.is_production)
        for v in versions
    ] == [
        # The shipped prompt has no row: first seen on its first run.
        (SHIPPED, f"production {SHIPPED[:8]}", "ana", True, True),
        (ids["candidate_sha"], "causal-test", "ben", True, False),
        ("f" * 64, "sent", "cy", False, False),
        (prompt_sha256(later), "later", None, True, False),
    ]
    # The diff base is the closest older version with text.
    assert [v.previous_sha256 for v in versions] == [
        None,
        SHIPPED,
        ids["candidate_sha"],
        ids["candidate_sha"],
    ]
    assert [(v.runs, v.latest_run_id) for v in versions] == [
        (1, ids["shipped"]),
        (1, ids["candidate"]),
        (1, unrecorded),
        (0, None),
    ]

    # A shipped (global) row names its hash ahead of the org's row.
    session.add(
        QAPromptModel(
            stage=POST.value,
            sha256=ids["candidate_sha"],
            text=CANDIDATE,
            name="shipped once",
            source="production",
            git_author="dana",
        )
    )
    await session.flush()
    status = await _status(session, ids)
    assert status.runs[1].title == "shipped once"
    [candidate] = [v for v in status.versions if v.sha256 == ids["candidate_sha"]]
    assert (candidate.name, candidate.author) == ("shipped once", "dana")


@needs_db
@pytest.mark.asyncio
async def test_version_text_is_the_recorded_or_shipped_text(session):
    ids = await _seed(session)

    async def text(org_id: str, sha: str) -> str:
        version = await get_prompt_version_text_core(
            session, org_id=org_id, stage=POST, sha256=sha
        )
        return version.text

    assert await text(ids["org"], SHIPPED) == shipped_prompt(POST)
    assert await text(ids["org"], ids["candidate_sha"]) == CANDIDATE
    # Another org sees the shipped prompt but never this org's candidate.
    assert await text("other-org", SHIPPED) == shipped_prompt(POST)
    for org_id, sha in (("other-org", ids["candidate_sha"]), (ids["org"], "f" * 64)):
        with pytest.raises(HTTPException) as caught:
            await text(org_id, sha)
        assert caught.value.status_code == 404


@needs_db
@pytest.mark.asyncio
async def test_a_model_is_one_name_whatever_route_it_ran_through(session):
    ids = await _seed(session)
    t1 = ids["trials"][0]
    for minutes, model in (
        (10, "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
        (11, "anthropic/claude-haiku-4-5"),
        (12, "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        (13, "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
        (14, "anthropic/claude-haiku-4-5-20251001"),
    ):
        run = await _run(session, ids, model, minutes)
        session.add(
            _replay(ids, run, t1, SHIPPED, OK, GOOD, "BAD_FAILURE", 0.2, model=model)
        )
    await session.flush()
    status = await _status(session, ids)
    assert {r.model_name for r in status.runs} == {
        "claude-sonnet-5",
        "claude-haiku-4-5",
    }
    haiku = next(m for m in status.models if m.model_name == "claude-haiku-4-5")
    assert haiku.runs == 5


def _summary(run_id: str, cost: float | None, accuracy: float) -> QAEvalRunSummary:
    return QAEvalRunSummary(
        experiment_id=run_id,
        experiment_name=run_id,
        title=run_id,
        created_at=utcnow(),
        model=run_id,
        model_name=run_id.split("-")[0],
        prompt_sha256="p",
        subset=False,
        charted=True,
        replays=1,
        with_golden=1,
        judged=1,
        correct=0,
        errored=0,
        pending=0,
        accuracy=accuracy,
        cost_usd=cost or 0,
        cost_per_trial=cost,
        cost_missing=0,
    )


def test_the_frontier_keeps_runs_no_cheaper_run_matches():
    runs = [
        _summary("cheap-bad", 0.5, 0.48),
        _summary("mid-good", 0.6, 0.88),
        _summary("tie-cost-worse", 0.6, 0.7),
        _summary("dear-same", 1.0, 0.88),
        _summary("dear-worse", 1.1, 0.8),
        _summary("unpriced", None, 1.0),
    ]
    assert _frontier(runs) == ["cheap-bad", "mid-good"]


def test_models_rank_by_latest_accuracy_then_cost_with_no_cost_last():
    runs = [
        _summary("a-old", 0.1, 1.0),
        _summary("a-new", 0.9, 0.8),
        _summary("b-1", None, 0.8),
        _summary("c-1", 0.3, 0.8),
        _summary("d-1", 5.0, 0.9),
    ]
    assert [(m.model_name, m.runs, m.latest_run_id) for m in _models(runs)] == [
        ("d", 1, "d-1"),
        ("c", 1, "c-1"),
        ("a", 2, "a-new"),
        ("b", 1, "b-1"),
    ]


def test_model_comparison_keeps_harness_and_effort_distinct():
    base = _summary("claude-high", 0.5, 0.8)
    base.reasoning_effort = "high"
    other_agent = base.model_copy(update={"experiment_id": "mini-high", "agent": "mini-swe-agent"})
    other_effort = base.model_copy(update={"experiment_id": "claude-low", "reasoning_effort": "low"})
    assert len(_models([base, other_agent, other_effort])) == 3


@needs_db
@pytest.mark.asyncio
async def test_saved_provider_error_remains_visible_after_cancellation(session):
    ids = await _seed(session)
    trial = await session.scalar(select(TrialModel).where(
        TrialModel.experiment_id == ids["candidate"], TrialModel.status == BAD,
    ))
    trial.error_message = trial.analysis_error = "Cancelled by user"
    trial.result = {"harbor_exception": {
        "exception_type": "ApiClientError", "error": "ApiClientError: model identifier is invalid",
    }}
    await session.flush()
    run = await get_qa_eval_run_core(session, org_id=ids["org"], experiment_id=ids["candidate"])
    [case] = [case for case in run.cases if case.qa_eval_trial_id == trial.id]
    assert case.error == "ApiClientError: model identifier is invalid"


@pytest.mark.parametrize("model", ["gpt-6-luna", "claude-sonnet-5", "gemini-3-pro", "glm-5", "grok-4"])
def test_model_aliases_with_different_execution_hashes_share_latest_score(model):
    old = _summary("old", 0.2, 0.92).model_copy(update={
        "model": f"provider/{model}", "model_name": _model_name(f"provider/{model}"),
        "reasoning_effort": "high", "configuration_sha256": "old-fingerprint",
    })
    new = old.model_copy(update={
        "experiment_id": "new", "model": model, "model_name": _model_name(model),
        "configuration_sha256": "new-fingerprint", "accuracy": 0.8,
    })
    assert old.model_name == new.model_name
    rows = _models([old, new])
    assert len(rows) == 1
    assert rows[0].latest_run_id == "new"
    assert rows[0].runs == 2
    assert old.configuration_sha256 == "old-fingerprint"
    assert new.configuration_sha256 == "new-fingerprint"
