"""Live QA issue reports for deliveries and experiments against real PostgreSQL."""

import re
from contextlib import asynccontextmanager
from importlib.resources import files

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.qa_issue_taxonomy import CATEGORIES, LABEL_CATEGORY
from oddish.core.qa_burndown import qa_burndown
from oddish.core.qa_issue_report import qa_issue_report_core
from oddish.db import ExperimentModel, TaskVersionModel, generate_id
from oddish.db.models import task_experiments
from test_deliveries import ORG, _trial
from test_delivery_qa import _reviewed_delivery


def _labels(subtype: str, classification: str = "BAD_FAILURE") -> dict:
    return {
        "classification": classification,
        "subtype": subtype,
        "evidence": "test_api expects `get_user`",
        "root_cause": "The tests need a name the instruction never gives.",
        "recommendation": "Name the function in the instruction.",
    }


def _finding(dimension: str, tier: str = "must_fix") -> dict:
    return {
        "source": "pre_trial",
        "problem_type": "mismatch",
        "dimension": dimension,
        "file": "tests/expected.json",
        "line_start": 1,
        "line_end": 1,
        "title": f"A {dimension} defect.",
        "detail": "x",
        "recommendation": "y",
        "tier": tier,
    }


async def _report(session, scope_id: str, category=None, kind="delivery", **kw):
    return await qa_issue_report_core(
        session, scope_kind=kind, scope_id=scope_id, org_id=ORG, category=category, **kw
    )


def test_every_task_problem_subtype_has_one_category():
    labels = [label for c in CATEGORIES for label in c.labels]
    assert len(labels) == len(set(labels))
    prompt = files("oddish.analyze").joinpath("classify_prompt.txt").read_text()
    bad = re.findall(r"### BAD_\w+.*?(?=\n### |\n---)", prompt, re.S)
    subtypes = set(re.findall(r"^\* `(\w+)`", "".join(bad), re.M))
    assert len(subtypes) > 10
    assert subtypes <= set(LABEL_CATEGORY)


@pytest.mark.asyncio
async def test_rejected_task_takes_its_trials_category(session):
    delivery, task, _, _, _, sources = await _reviewed_delivery(session)
    for source in sources[:3]:
        source.analysis = _labels("unstated_interface")
    task.verdict = {"verdict": "reject", "is_good": False, "primary_issue": "3/5"}
    await session.flush()

    overview = await _report(session, delivery.id)
    assert overview.summary == {"tasks": 1, "blocked": 1, "clear": 0, "waiting": 0}
    assert overview.tasks == []
    [category] = overview.categories
    assert category["slug"] == "tests-demand-unstated-details"
    assert (category["issues"], category["blocking_issues"]) == (4, 1)

    detail = await _report(session, delivery.id, category["slug"])
    [row] = detail.tasks
    assert [i["kind"] for i in row["issues"]] == ["verdict", "trial", "trial", "trial"]
    assert (await _report(session, delivery.id, "other")).tasks == []
    blocking = await _report(session, delivery.id, category["slug"], blocking=True)
    assert [i["kind"] for i in blocking.tasks[0]["issues"]] == ["verdict"]
    missing = await _report(session, delivery.id, category["slug"], q="no such text")
    assert (missing.task_total, missing.tasks) == (0, [])
    later = await _report(session, delivery.id, category["slug"], page=2)
    assert (later.task_total, later.tasks) == (1, [])


@pytest.mark.asyncio
async def test_reject_without_evidence_has_its_own_category(session):
    delivery, task, *_ = await _reviewed_delivery(session)
    task.verdict = {"verdict": "reject", "is_good": False, "primary_issue": "odd"}
    await session.flush()

    report = await _report(session, delivery.id)

    assert [c["slug"] for c in report.categories] == ["rejected-reason-unknown"]


@pytest.mark.asyncio
async def test_must_fix_finding_replaces_the_verdict_issue(session):
    delivery, task, version, _, _, _ = await _reviewed_delivery(session)
    version.pre_trial = {
        "items": [_finding("info_leakage"), _finding("verifier", tier="optional")]
    }
    task.verdict = {"verdict": "reject", "is_good": False, "primary_issue": "leak"}
    await session.flush()

    report = await _report(session, delivery.id, "agent-can-read-hidden-files")

    [row] = report.tasks
    [issue] = row["issues"]
    assert (issue["kind"], issue["blocking"]) == ("finding", True)
    assert [c["slug"] for c in report.categories] == ["agent-can-read-hidden-files"]


@pytest.mark.asyncio
async def test_blocking_finding_blocks_before_qa_finishes(session):
    delivery, _, version, _, qa, sources = await _reviewed_delivery(session)
    sources[0].analysis = _labels("nondeterministic_tests")
    qa.harbor_config = {}  # an unparseable pin makes the QA run outdated
    await session.flush()
    assert (await _report(session, delivery.id)).summary["waiting"] == 1

    version.pre_trial = {"items": [_finding("info_leakage")]}
    await session.flush()
    report = await _report(session, delivery.id)

    assert (report.summary["blocked"], report.summary["waiting"]) == (1, 0)
    [leak] = [c for c in report.categories if c["blocking_issues"]]
    assert leak["slug"] == "agent-can-read-hidden-files"


@pytest.mark.asyncio
async def test_experiment_report_counts_only_its_own_trials(session):
    _, task, version, experiment, _, sources = await _reviewed_delivery(session)
    other = ExperimentModel(name=f"other-{generate_id()}", org_id=ORG)
    session.add(other)
    await session.flush()
    await session.execute(
        task_experiments.insert().values(
            [
                {"task_id": task.id, "experiment_id": experiment.id},
                {"task_id": task.id, "experiment_id": other.id},
            ]
        )
    )
    sources[0].analysis = _labels("hardcoding", "BAD_SUCCESS")
    session.add(_trial(task, other, version.id, analysis=_labels("environment_defect")))
    await session.flush()

    report = await _report(session, experiment.id, kind="experiment")

    assert [c["slug"] for c in report.categories] == ["agent-can-hard-code-answers"]
    with pytest.raises(HTTPException):
        await qa_issue_report_core(
            session, scope_kind="experiment", scope_id=experiment.id, org_id="x"
        )


@pytest_asyncio.fixture
async def report_api(monkeypatch):
    import oddish.db.connection as connection
    import oddish.server as server
    import oddish.core.sharing.public as public
    import oddish.server.qa_issue_reports as routes

    async with connection.engine.connect() as conn:
        transaction = await conn.begin()

        @asynccontextmanager
        async def get_session():
            async with AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        monkeypatch.setattr(routes, "get_read_session", get_session)
        monkeypatch.setattr(public, "get_read_session", get_session)
        async with get_session() as session:
            experiment = ExperimentModel(name=f"local-{generate_id()}", org_id=None)
            session.add(experiment)
            await session.commit()
            experiment_id = experiment.id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.api), base_url="http://test"
        ) as client:
            try:
                yield client, experiment_id
            finally:
                await transaction.rollback()


@pytest.mark.asyncio
async def test_standalone_route(report_api):
    client, experiment_id = report_api
    url = f"/experiments/{experiment_id}/qa-issues"

    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["tasks"] == 0
    assert (await client.get(url, params={"category": "nope"})).status_code == 404
    assert (await client.get("/deliveries/missing/qa-issues")).status_code == 404
    assert (await client.get("/public/experiments/nope/qa-issues")).status_code == 404


@pytest.mark.asyncio
async def test_public_report_needs_show_qa_and_hides_people(session):
    delivery, task, _, experiment, qa, sources = await _reviewed_delivery(session)
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
    )
    sources[0].analysis = _labels("hardcoding", "BAD_SUCCESS")
    task.tags = {"github_username": "secret-author"}
    task.verdict = {"verdict": "reject", "is_good": False, "_graded_by": qa.id}
    await session.flush()
    public = await qa_issue_report_core(
        session,
        scope_kind="experiment",
        scope_id=experiment.id,
        org_id=ORG,
        category="agent-can-hard-code-answers",
        public=True,
    )
    [row] = public.tasks
    assert [i["kind"] for i in row["issues"]] == ["verdict", "trial"]
    assert (row["author"], row["pr_url"]) == (None, None)
    assert [i["trial_id"] for i in row["issues"]] == [None, None]
    hidden = await qa_issue_report_core(
        session,
        scope_kind="experiment",
        scope_id=experiment.id,
        org_id=ORG,
        category="agent-can-hard-code-answers",
        q="secret-author",
        public=True,
    )
    assert hidden.tasks == []


@pytest.mark.asyncio
async def test_burndown_counts_each_task_at_each_version(session):
    _, task, version, _, _, _ = await _reviewed_delivery(session)
    version.pre_trial = {"items": [_finding("verifier")]}
    for number, is_good in ((2, False), (3, True)):
        session.add(
            TaskVersionModel(
                id=f"{task.id}-v{number}",
                task_id=task.id,
                version=number,
                task_path="x",
                verdict={"is_good": is_good},
            )
        )
    _, untouched, *_ = await _reviewed_delivery(session)
    await session.flush()
    live = {"blocked": 0, "clear": 1, "waiting": 1}

    points = await qa_burndown(session, [task.id, untouched.id], live)

    assert points == [
        {"version": 1, "blocked": 1, "clear": 0, "waiting": 1},
        {"version": 2, "blocked": 1, "clear": 0, "waiting": 1},
        {"version": 3, "blocked": 0, "clear": 1, "waiting": 1},
        {"version": None, **live},
    ]
