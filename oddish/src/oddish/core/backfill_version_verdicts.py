"""Recover each task version's own QA verdict from its QA runs.

``tasks.verdict`` keeps only the newest verdict, so a version reviewed before a
later version lost its verdict there. The QA run's ``qa_result.json`` is still
in trial storage. This rebuilds the verdict that run published, using the same
rules as the QA import, and stores it on the version. As in the import, a later
run on the version that published nothing (classification-only, or refused)
leaves the earlier verdict standing, so the newest publishing run wins.

Dry run by default: prints one line per version and a summary, writes nothing.
``--apply`` stores each recovered verdict as soon as it is rebuilt. Versions
that already have a verdict, including one published while this runs, are
never changed. Resume with --after-id using the
last logged version ID.

    uv run python -m oddish.core.backfill_version_verdicts --limit 500
    uv run python -m oddish.core.backfill_version_verdicts --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter

from sqlalchemy import select

from oddish.core.task_findings import task_defect_items
from oddish.db import TaskVersionModel, TrialModel, TrialStatus, get_session, utcnow
from oddish.analyze import TrialClassification
from oddish.workers.analysis_trials import (
    QA_RESULT_FILENAME,
    AnalysisPayloadError,
    _classification_from_analysis,
    analysis_check_payload,
    qa_verdict_outcome,
    read_analysis_artifact,
)
from oddish.worker.analysis_result_check import check_analysis_result

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 100


async def _next_version_ids(session, after_id: str | None, batch: int) -> list[str]:
    query = (
        select(TaskVersionModel.id)
        .where(
            TaskVersionModel.verdict.is_(None),
            TaskVersionModel.deleted_at.is_(None),
        )
        .order_by(TaskVersionModel.id.asc())
    )
    if after_id is not None:
        query = query.where(TaskVersionModel.id > after_id)
    rows = await session.execute(query.limit(batch))
    return [str(value) for value in rows.scalars().all()]


async def _qa_runs(session, version_id: str) -> list[TrialModel]:
    """The version's successful QA runs, newest first."""
    return list(
        (
            await session.execute(
                select(TrialModel)
                .where(
                    TrialModel.task_version_id == version_id,
                    TrialModel.kind == "qa",
                    TrialModel.status == TrialStatus.SUCCESS,
                    TrialModel.deleted_at.is_(None),
                )
                .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def recover_version_verdict(
    session, version: TaskVersionModel
) -> tuple[str, dict | None, str | None]:
    """(outcome, payload, detail) for one version, without writing.

    The newest run that publishes a verdict supplies it. When none does, the
    newest run's outcome says why.
    """
    newest = None
    for qa in await _qa_runs(session, version.id):
        result = await _run_verdict(session, version, qa)
        if result[0] == "recovered":
            return result
        newest = newest or result
    return newest or ("no_qa_run", None, None)


async def _run_verdict(
    session, version: TaskVersionModel, qa: TrialModel
) -> tuple[str, dict | None, str | None]:
    """The verdict one QA run published, rebuilt as the import builds it."""
    try:
        expected = analysis_check_payload("qa", qa.harbor_config)
    except AnalysisPayloadError as exc:
        return "invalid_payload", None, f"{qa.id}: {exc}"
    artifact = await read_analysis_artifact(qa, QA_RESULT_FILENAME)
    if artifact is None:
        return "no_artifact", None, qa.id
    violations = check_analysis_result(artifact, expected)
    if violations:
        return "invalid_artifact", None, f"{qa.id}: {violations[0]}"
    # As the import does: skip graded trials deleted since, refuse the rest.
    classifications: list[TrialClassification] = []
    for entry in artifact["trials"]:
        row = await session.get(TrialModel, entry["trial_id"])
        if row is None or row.task_id != qa.task_id:
            continue
        parsed = _classification_from_analysis(
            entry["analysis"],
            trial_name=row.id,
            reward=float(row.reward) if row.reward is not None else None,
        )
        if parsed is None:
            return "invalid_artifact", None, f"{qa.id}: {row.id} failed to parse"
        classifications.append(parsed)
    if not classifications:
        return "no_verdict", None, f"{qa.id}: no valid classifications"
    reports = (await task_defect_items(session, {version.id: version}))[version.id]
    baseline_errors = {}
    for item in expected.get("baseline_evidence") or []:
        baseline = await session.get(TrialModel, item["trial_id"])
        baseline_errors[item["trial_id"]] = getattr(baseline, "error_message", None)
    payload, error = qa_verdict_outcome(
        qa,
        artifact,
        expected,
        classifications,
        task_defect_count=len(reports),
        baseline_errors=baseline_errors,
    )
    if payload is None and error is None:
        # Classification-only, with no deterministic rejection to publish.
        return "classification_only", None, qa.id
    if payload is None:
        return "no_verdict", None, f"{qa.id}: {error}"
    return "recovered", {**payload, "_backfilled_at": utcnow().isoformat()}, qa.id


async def backfill(
    *,
    apply: bool,
    limit: int | None = None,
    after_id: str | None = None,
    batch: int = DEFAULT_BATCH,
) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    seen = 0
    while limit is None or seen < limit:
        page = batch if limit is None else min(batch, limit - seen)
        async with get_session() as session:
            version_ids = await _next_version_ids(session, after_id, page)
            if not version_ids:
                break
            for version_id in version_ids:
                version = await session.get(TaskVersionModel, version_id)
                if version is None or version.verdict is not None:
                    continue
                outcome, payload, detail = await recover_version_verdict(
                    session, version
                )
                outcomes[outcome] += 1
                logger.info("%s %s %s", version_id, outcome, detail or "")
                if apply and payload is not None:
                    # Lock only for the write, so a live QA import on this
                    # version waits for one row, not a batch of storage reads.
                    await session.refresh(version, with_for_update=True)
                    if version.verdict is None:
                        version.verdict = payload
                    await session.commit()
            await session.rollback()
        seen += len(version_ids)
        after_id = version_ids[-1]
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="Store recovered verdicts."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--after-id", default=None)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outcomes = asyncio.run(
        backfill(
            apply=args.apply,
            limit=args.limit,
            after_id=args.after_id,
            batch=args.batch,
        )
    )
    mode = "applied" if args.apply else "dry run"
    logger.info("%s: %s", mode, dict(sorted(outcomes.items())))


if __name__ == "__main__":
    main()
