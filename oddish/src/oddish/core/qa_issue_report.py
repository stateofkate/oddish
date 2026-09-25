"""Live QA issue report for one experiment or one delivery.

A report reads the QA output Oddish already stores and puts every issue in
one plain-English category (``oddish.analyze.qa_issue_taxonomy``). No model
call runs and nothing is stored. An issue is one of:

* a recorded ``must_fix`` finding on the task's current version (blocking);
* a trial the QA agent labeled as a task problem (not blocking alone);
* a current reject verdict on a task with no ``must_fix`` finding (blocking),
  so every rejected task shows a blocking issue in the table.

"Finished QA" is the delivery board's rule: the latest QA run is current and
its verdict is published (``accepted`` or ``needs_fixes``). The headline
splits tasks into blocked (any blocking issue, finished QA or not), clear
(finished QA, no blocking issue), and waiting (the rest).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.qa_issue_taxonomy import (
    CATEGORIES,
    CATEGORY_BY_SLUG,
    DIMENSIONS,
    TAXONOMY_VERSION,
    category_for_finding,
    category_for_rejection,
    category_for_subtype,
    plain_label,
)
from oddish.core.delivery_qa import delivery_qa_statuses
from oddish.core.deliveries import _get_delivery, _member_rows
from oddish.core.experiment_membership import trial_in_experiment
from oddish.core.helpers import _parse_github_meta
from oddish.core.task_findings import task_defect_items
from oddish.db import ExperimentModel, TaskModel, TaskVersionModel, TrialModel
from oddish.db.models import task_experiments
from oddish.filters.trial_predicates import qa_eligible_trial_clauses
from oddish.schemas import QAIssueReportResponse

FINISHED_QA = frozenset({"accepted", "needs_fixes"})
_SOURCE_LABELS = {"pre_trial": "Source audit", "trial": "Trial review"}


def _text(*parts: object) -> str:
    return "\n\n".join(str(p).strip() for p in parts if p and str(p).strip() != "N/A")


def _finding_issue(defect: dict, version: TaskVersionModel) -> dict[str, Any]:
    finding = defect.get("finding") or {}
    source = str(defect.get("source") or "finding")
    return {
        "category": category_for_finding(finding),
        "kind": "finding",
        "blocking": True,
        "severity": "must_fix",
        "found_by": _SOURCE_LABELS.get(source, plain_label(source)),
        "label": f"{finding.get('dimension')}/{finding.get('problem_type')}",
        "title": defect.get("title") or "",
        "text": _text(finding.get("detail"), finding.get("recommendation")),
        "file": finding.get("file"),
        "trial_id": defect.get("reporting_trial_id"),
        "date": version.pre_trial_finished_at if source == "pre_trial" else None,
    }


def _trial_issue(trial: Any) -> dict[str, Any]:
    analysis = trial.analysis or {}
    subtype = str(analysis.get("subtype") or "")
    return {
        "category": category_for_subtype(subtype),
        "kind": "trial",
        "blocking": False,
        "severity": analysis.get("classification"),
        "found_by": f"QA of trial ({trial.agent})",
        "label": subtype,
        "title": str(analysis.get("root_cause") or plain_label(subtype)),
        "text": _text(analysis.get("evidence"), analysis.get("recommendation")),
        "trial_id": trial.id,
        "date": trial.finished_at,
    }


def _verdict_issue(task: TaskModel, trial_issues: list[dict]) -> dict[str, Any]:
    verdict = task.verdict or {}
    primary = verdict.get("primary_issue")
    # A reject without a finding rests on a failed baseline or on most trials
    # showing the same problem, so take the trials' most common category.
    counts = Counter(i["category"] for i in trial_issues)
    category = category_for_rejection(primary) or (
        counts.most_common(1)[0][0] if counts else "rejected-reason-unknown"
    )
    return {
        "category": category,
        "kind": "verdict",
        "blocking": True,
        "severity": "reject",
        "found_by": "QA verdict",
        "label": "reject",
        "title": str(primary or "QA rejected the task."),
        "text": _text(
            verdict.get("reasoning"), "\n".join(verdict.get("recommendations") or [])
        ),
        "trial_id": verdict.get("_graded_by"),
        "date": task.verdict_finished_at,
    }


async def _trial_issues(
    session: AsyncSession, version_ids: list[str], experiment_id: str | None
) -> dict[str, list[dict]]:
    query = (
        select(
            TrialModel.id,
            TrialModel.task_id,
            TrialModel.agent,
            TrialModel.analysis,
            TrialModel.finished_at,
        )
        .where(
            TrialModel.task_version_id.in_(version_ids),
            *qa_eligible_trial_clauses(),
            or_(
                TrialModel.analysis["classification"].astext.in_(
                    ["BAD_FAILURE", "BAD_SUCCESS"]
                ),
                TrialModel.analysis["subtype"].astext == "hidden_file_leak",
            ),
        )
        .order_by(TrialModel.task_id, TrialModel.id)
    )
    if experiment_id is not None:
        query = query.where(trial_in_experiment(experiment_id))
    issues: dict[str, list[dict]] = defaultdict(list)
    for trial in await session.execute(query):
        issues[trial.task_id].append(_trial_issue(trial))
    return issues


def _category_tasks(
    rows: list[dict[str, Any]], category: str, blocking: bool, q: str
) -> list[dict[str, Any]]:
    needle = q.strip().lower()
    out = []
    for row in rows:
        issues = [
            i
            for i in row["issues"]
            if i["category"] == category and (i["blocking"] or not blocking)
        ]
        text = " ".join(
            [row["task_name"], row["author"] or "", str(row["pr_number"] or "")]
            + [f"{i['title']} {i['text']}" for i in issues]
        ).lower()
        if issues and needle in text:
            out.append({**row, "issues": issues})
    return out


async def _scope_tasks(
    session: AsyncSession, scope_kind: str, scope_id: str, org_id: str | None
) -> tuple[str, list[TaskModel]]:
    if scope_kind == "delivery":
        delivery = await _get_delivery(session, scope_id, org_id)
        ids = [row.task_id for row in await _member_rows(session, scope_id)]
        where = TaskModel.id.in_(ids)
        name = delivery.name
    else:
        experiment = await session.get(ExperimentModel, scope_id)
        if (
            experiment is None
            or experiment.org_id != org_id
            or experiment.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Experiment not found")
        where = TaskModel.id.in_(
            select(task_experiments.c.task_id).where(
                task_experiments.c.experiment_id == scope_id,
                task_experiments.c.deleted_at.is_(None),
            )
        )
        name = experiment.name
    return name, list(await session.scalars(select(TaskModel).where(where)))


async def qa_issue_report_core(
    session: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    org_id: str | None,
    category: str | None = None,
    blocking: bool = False,
    q: str = "",
    page: int = 1,
    page_size: int = 25,
    public: bool = False,
) -> QAIssueReportResponse:
    """Build the report live. Task rows are sent only for one ``category``.

    Each task carries a few KB of QA text, so the full report for a large
    scope is several MB. The overview needs only the counts and categories.
    ``blocking``, ``q`` (task, author, PR number, or issue text), and
    ``page`` narrow that category's task rows on the server, so a response
    carries at most ``page_size`` tasks however large the scope is.
    ``public`` drops authors, PR links, and QA trial ids, like other share
    views, so a public search cannot match them either.
    Experiment reports count only trials in the experiment; findings and
    verdicts describe the task version, so they are not limited.
    """
    if category is not None and category not in CATEGORY_BY_SLUG:
        raise HTTPException(status_code=404, detail="Unknown QA issue category")
    name, tasks = await _scope_tasks(session, scope_kind, scope_id, org_id)
    version_ids = [t.current_version_id for t in tasks if t.current_version_id]
    versions = {
        v.id: v
        for v in await session.scalars(
            select(TaskVersionModel).where(TaskVersionModel.id.in_(version_ids))
        )
    }
    qa = await delivery_qa_statuses(
        session, tasks={t.id: t for t in tasks}, versions=versions
    )
    defects = await task_defect_items(session, versions)
    trial_issues = await _trial_issues(
        session, version_ids, scope_id if scope_kind == "experiment" else None
    )

    rows: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda t: t.name):
        version = versions.get(task.current_version_id or "")
        status = qa[task.id].status if task.id in qa else "never"
        findings = [
            _finding_issue(d, version)
            for d in (defects.get(version.id, []) if version else [])
            if d.get("recorded_tier") == "must_fix"
        ]
        issues = findings + trial_issues.get(task.id, [])
        if status == "needs_fixes" and not findings:
            issues.insert(0, _verdict_issue(task, trial_issues.get(task.id, [])))
        if public:
            issues = [{**i, "trial_id": None} for i in issues]
        meta = {} if public else _parse_github_meta(task.tags) or {}
        rows.append(
            {
                "task_id": task.id,
                "task_name": task.name,
                "version_id": task.current_version_id,
                "qa_status": status,
                "finished_qa": status in FINISHED_QA,
                "author": (
                    None
                    if public
                    else (task.tags or {}).get("github_username") or task.user
                ),
                "pr_url": meta.get("pr_url"),
                "pr_number": meta.get("pr_number"),
                "blocking_issues": sum(1 for i in issues if i["blocking"]),
                "issues": issues,
            }
        )

    categories: list[dict[str, Any]] = []
    for cat in CATEGORIES:
        hits = [(r, i) for r in rows for i in r["issues"] if i["category"] == cat.slug]
        if hits:
            categories.append(
                {
                    "slug": cat.slug,
                    "name": cat.name,
                    "dimension": DIMENSIONS[cat.dimension],
                    "description": cat.description,
                    "issues": len(hits),
                    "blocking_issues": sum(1 for _, i in hits if i["blocking"]),
                    "tasks": len({r["task_id"] for r, _ in hits}),
                }
            )
    categories.sort(key=lambda c: (-c["issues"], c["name"]))

    blocked = sum(1 for r in rows if r["blocking_issues"])
    clear = sum(1 for r in rows if r["finished_qa"] and not r["blocking_issues"])
    matches = _category_tasks(rows, category, blocking, q) if category else []
    return QAIssueReportResponse(
        scope_kind=scope_kind,
        scope_id=scope_id,
        scope_name=name,
        taxonomy_version=TAXONOMY_VERSION,
        summary={
            "tasks": len(rows),
            "blocked": blocked,
            "clear": clear,
            "waiting": len(rows) - blocked - clear,
        },
        categories=categories,
        category=category,
        task_total=len(matches),
        tasks=matches[(page - 1) * page_size : page * page_size],
    )
