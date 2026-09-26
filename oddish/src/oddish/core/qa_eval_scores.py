"""Scores of QA prompt replays against the counted goldens: what the QA Evals
page and ``oddish qa evals`` show.

Nothing is stored. A run is one ``/qa-evals`` replay experiment; each read
loads the stage's replays and goldens once and scores every case, asking the
stage's ``StageSpec`` how to grade.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oddish.analyze.qa_stages import (
    STAGES,
    QAStage,
    shipped_prompt,
    shipped_prompt_sha256,
)
from oddish.config import (
    model_family_key,
    to_anthropic_api_model_id,
    to_bedrock_model_id,
)
from oddish.core.qa_goldens import golden_state, original_id
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    QAGoldenCaseModel,
    QAPromptModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.schemas import (
    QAEvalCase,
    QAEvalErrorGroup,
    QAEvalGroup,
    QAEvalModelRow,
    QAEvalModelSpend,
    QAEvalOutcome,
    QAEvalRunResponse,
    QAEvalRunSummary,
    QAEvalSpend,
    QAEvalStatusResponse,
    QAGoldensSummary,
    QALabelCount,
    QAPromptVersion,
    QAPromptVersionText,
)

REPLAY_PAYLOAD = TrialModel.harbor_config["analysis_payload"]
_SOURCE_ID = REPLAY_PAYLOAD["trial_ids"][0].astext
# Replays stored before the payload named its stage are all post-trial.
REPLAY_STAGE = func.coalesce(REPLAY_PAYLOAD["stage"].astext, QAStage.POST_TRIAL.value)

_ERRORED = (TrialStatus.FAILED, TrialStatus.SKIPPED)
_GROUPS: dict[QAEvalOutcome, QAEvalGroup] = {
    "right": "right",
    "wrong": "wrong",
    "invalid": "wrong",
    "errored": "not_judged",
    "pending": "not_judged",
    "off": "no_golden",
}

# File paths differ per trial (sandbox job directories), so they are masked
# before grouping: "No reward file found at <path>" is one group, not 15.
_PATH = re.compile(r"(?:/[\w.@+-]+){2,}/?")


def _shipped_name(stage: QAStage) -> str:
    return f"production {shipped_prompt_sha256(stage)[:8]}"


async def _prompt_rows(
    session: AsyncSession, org_id: str | None, stage: QAStage, sha256: str | None
) -> dict[str, QAPromptModel]:
    """Recorded versions visible to the org by hash. A shipped (global) row
    wins over the org's copy of the same text."""
    query = select(QAPromptModel).where(
        QAPromptModel.stage == stage.value,
        or_(QAPromptModel.org_id == org_id, QAPromptModel.org_id.is_(None)),
    )
    if sha256 is not None:
        query = query.where(QAPromptModel.sha256 == sha256)
    rows: dict[str, QAPromptModel] = {}
    for row in await session.scalars(
        query.order_by(QAPromptModel.org_id.is_not(None), QAPromptModel.created_at)
    ):
        rows.setdefault(row.sha256, row)
    return rows


async def prompt_version(
    session: AsyncSession, org_id: str | None, stage: QAStage, sha256: str
) -> tuple[str, str] | None:
    """The name and text of a recorded or shipped prompt version."""
    row = (await _prompt_rows(session, org_id, stage, sha256)).get(sha256)
    if row is not None:
        return row.name, row.text
    if sha256 == shipped_prompt_sha256(stage):
        return _shipped_name(stage), shipped_prompt(stage)
    return None


async def get_prompt_version_text_core(
    session: AsyncSession, *, org_id: str | None, stage: QAStage, sha256: str
) -> QAPromptVersionText:
    version = await prompt_version(session, org_id, stage, sha256)
    if version is None:
        raise HTTPException(
            status_code=404, detail=f"no recorded {stage.value} prompt {sha256}"
        )
    return QAPromptVersionText(sha256=sha256, text=version[1])


_Golden = tuple[str, QAGoldenCaseModel, str]


async def _goldens(
    session: AsyncSession, org_id: str | None, stage: QAStage
) -> list[_Golden]:
    """Live goldens whose subject still exists, as (subject id, golden,
    state)."""
    if STAGES[stage].subject == "trial":
        subject = QAGoldenCaseModel.source_trial_id
        exists = select(TrialModel.id).where(TrialModel.id == subject).exists()
    else:
        subject = QAGoldenCaseModel.task_version_id
        exists = (
            select(TaskVersionModel.id).where(TaskVersionModel.id == subject).exists()
        )
    result = await session.execute(
        select(subject, QAGoldenCaseModel, golden_state()).where(
            QAGoldenCaseModel.org_id == org_id,
            QAGoldenCaseModel.stage == stage.value,
            exists,
        )
    )
    return [(subject, golden, state) for subject, golden, state in result.all()]


def _counted(goldens: list[_Golden]) -> dict[str, QAGoldenCaseModel]:
    return {subject: golden for subject, golden, state in goldens if state == "counted"}


async def counted_goldens(
    session: AsyncSession, org_id: str | None, stage: QAStage
) -> dict[str, QAGoldenCaseModel]:
    """The goldens that score runs, by subject id."""
    return _counted(await _goldens(session, org_id, stage))


async def _replays(
    session: AsyncSession,
    org_id: str | None,
    *,
    stage: QAStage | None = None,
    experiment_id: str | None = None,
) -> Sequence[Row]:
    """Live replays in live experiments, run by run, oldest run first."""
    source = aliased(TrialModel)
    query = (
        select(
            TrialModel.id,
            TrialModel.task_id,
            TrialModel.task_version_id,
            TrialModel.model,
            TrialModel.status,
            TrialModel.analysis_status,
            TrialModel.analysis,
            TrialModel.cost_usd,
            func.coalesce(TrialModel.analysis_error, TrialModel.error_message).label(
                "error"
            ),
            REPLAY_STAGE.label("stage"),
            _SOURCE_ID.label("source_trial_id"),
            # A replay of a copied trial scores as the original; a source
            # trial gone from the table keeps its own id.
            func.coalesce(original_id(source), _SOURCE_ID).label("original_trial_id"),
            REPLAY_PAYLOAD["prompt_sha256"].astext.label("prompt_sha256"),
            func.coalesce(REPLAY_PAYLOAD["prompt_name"].astext, "").label(
                "prompt_name"
            ),
            func.coalesce(REPLAY_PAYLOAD["subset"].as_boolean(), False).label("subset"),
            ExperimentModel.id.label("experiment_id"),
            ExperimentModel.name.label("experiment_name"),
            ExperimentModel.created_at,
            ExperimentModel.owner_user_id,
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        .outerjoin(source, source.id == _SOURCE_ID)
        .where(
            TrialModel.kind == "qa_eval",
            TrialModel.org_id == org_id,
            TrialModel.superseded_by_trial_id.is_(None),
            ExperimentModel.org_id == org_id,
            REPLAY_PAYLOAD["prompt_sha256"].astext.is_not(None),
        )
        .order_by(ExperimentModel.created_at, ExperimentModel.id, TrialModel.id)
    )
    if stage is not None:
        query = query.where(REPLAY_STAGE == stage.value)
    if experiment_id is not None:
        query = query.where(TrialModel.experiment_id == experiment_id)
    return (await session.execute(query)).all()


def _pending(replay: Row) -> bool:
    return replay.status not in _ERRORED and (
        replay.status != TrialStatus.SUCCESS or replay.analysis_status is None
    )


def _case(replay: Row, stage: QAStage, golden: QAGoldenCaseModel | None) -> QAEvalCase:
    spec = STAGES[stage]
    assert spec.grade is not None
    got = None
    # What the replay did, whether or not a golden scores it.
    result: QAEvalOutcome
    if replay.status in _ERRORED:
        result = "errored"
    elif _pending(replay):
        result = "pending"
    else:
        got = spec.grade.answer(
            replay.analysis
            if replay.analysis_status == AnalysisStatus.SUCCESS
            else None
        )
        if got is None:
            result = "invalid"
        elif golden is not None and spec.grade.is_correct(golden.expected, got):
            result = "right"
        else:
            result = "wrong"
    outcome: QAEvalOutcome = result if golden is not None else "off"
    return QAEvalCase(
        qa_eval_trial_id=replay.id,
        task_id=replay.task_id,
        source_trial_id=replay.source_trial_id or "",
        original_trial_id=replay.original_trial_id or "",
        group=_GROUPS[outcome],
        outcome=outcome,
        golden_id=golden.id if golden is not None else None,
        expected=spec.label(golden.expected) if golden is not None else None,
        got=got,
        cost_usd=float(replay.cost_usd) if replay.cost_usd is not None else None,
        error=replay.error if result in ("invalid", "errored") else None,
    )


def _title(names: dict[str, str], sha256: str, prompt_name: str) -> str:
    return names.get(sha256) or prompt_name or sha256[:8]


def _model_name(model: str | None) -> str:
    """One name per model, whatever route or dated alias it ran under."""
    try:
        model = to_bedrock_model_id(model)
    except ValueError:
        pass  # an unmapped Claude id keeps its own name
    return model_family_key(to_anthropic_api_model_id(model))


def _summary(
    replays: list[Row], cases: list[QAEvalCase], title: str
) -> QAEvalRunSummary:
    first = replays[0]
    outcomes = Counter(case.outcome for case in cases)
    judged = outcomes["right"] + outcomes["wrong"] + outcomes["invalid"]
    pending = sum(_pending(replay) for replay in replays)
    costs = [case.cost_usd for case in cases if case.cost_usd is not None]
    cost = sum(costs)
    return QAEvalRunSummary(
        experiment_id=first.experiment_id,
        experiment_name=first.experiment_name,
        title=title,
        created_at=first.created_at,
        model=first.model,
        model_name=_model_name(first.model),
        prompt_sha256=first.prompt_sha256,
        subset=first.subset,
        charted=not first.subset and judged > 0 and pending == 0,
        replays=len(cases),
        with_golden=len(cases) - outcomes["off"],
        judged=judged,
        correct=outcomes["right"],
        errored=outcomes["errored"],
        pending=pending,
        accuracy=outcomes["right"] / judged if judged else None,
        cost_usd=cost,
        cost_per_trial=cost / len(costs) if costs else None,
        cost_missing=sum(
            1
            for replay, case in zip(replays, cases, strict=True)
            if case.cost_usd is None and not _pending(replay)
        ),
    )


def _error_key(error: str | None) -> str:
    first_line = (error or "no error message").strip().splitlines()[0]
    return _PATH.sub("<path>", first_line)[:300]


def _error_groups(cases: list[QAEvalCase]) -> list[QAEvalErrorGroup]:
    counts = Counter(
        _error_key(case.error) for case in cases if case.outcome == "errored"
    )
    return [
        QAEvalErrorGroup(message=message, count=count)
        for message, count in counts.most_common()
    ]


def _score_runs(
    replays: Sequence[Row],
    stage: QAStage,
    goldens: dict[str, QAGoldenCaseModel],
    names: dict[str, str],
) -> list[QAEvalRunResponse]:
    """Each run scored case by case, oldest first."""
    by_run: dict[str, list[Row]] = defaultdict(list)
    for replay in replays:
        by_run[replay.experiment_id].append(replay)
    by_trial = STAGES[stage].subject == "trial"
    runs = []
    for group in by_run.values():
        cases = [
            _case(
                replay,
                stage,
                goldens.get(
                    replay.original_trial_id if by_trial else replay.task_version_id
                ),
            )
            for replay in group
        ]
        title = _title(names, group[0].prompt_sha256, group[0].prompt_name)
        runs.append(
            QAEvalRunResponse(
                summary=_summary(group, cases, title),
                error_groups=_error_groups(cases),
                cases=cases,
            )
        )
    return runs


def _names(stage: QAStage, rows: dict[str, QAPromptModel]) -> dict[str, str]:
    names = {sha: row.name for sha, row in rows.items() if row.name}
    names.setdefault(shipped_prompt_sha256(stage), _shipped_name(stage))
    return names


async def get_qa_eval_run_core(
    session: AsyncSession, *, org_id: str | None, experiment_id: str
) -> QAEvalRunResponse:
    replays = await _replays(session, org_id, experiment_id=experiment_id)
    if not replays:
        raise HTTPException(
            status_code=404, detail=f"QA eval run {experiment_id} not found"
        )
    stage = QAStage(replays[0].stage)
    goldens = await counted_goldens(session, org_id, stage)
    names = _names(stage, await _prompt_rows(session, org_id, stage, None))
    [run] = _score_runs(replays, stage, goldens, names)
    return run


def _frontier(charted: list[QAEvalRunSummary]) -> list[str]:
    """Runs no cheaper run matches on accuracy, cheapest first."""
    frontier: list[str] = []
    best = -1.0
    for run in sorted(
        (run for run in charted if run.cost_per_trial is not None),
        key=lambda run: (run.cost_per_trial, -(run.accuracy or 0)),
    ):
        assert run.accuracy is not None
        if run.accuracy > best:
            frontier.append(run.experiment_id)
            best = run.accuracy
    return frontier


def _models(charted: list[QAEvalRunSummary]) -> list[QAEvalModelRow]:
    """Each model's latest charted run, most accurate first, then cheapest;
    a run with no cost ranks after those with one."""
    by_model: dict[str, list[QAEvalRunSummary]] = defaultdict(list)
    for run in charted:
        by_model[run.model_name].append(run)
    ranked = sorted(
        by_model.values(),
        key=lambda runs: (
            -(runs[-1].accuracy or 0),
            runs[-1].cost_per_trial is None,
            runs[-1].cost_per_trial or 0,
        ),
    )
    return [
        QAEvalModelRow(
            model_name=runs[-1].model_name,
            runs=len(runs),
            latest_run_id=runs[-1].experiment_id,
        )
        for runs in ranked
    ]


def _versions(
    stage: QAStage,
    rows: dict[str, QAPromptModel],
    names: dict[str, str],
    runs: list[QAEvalRunSummary],
    owners: dict[str, str | None],
) -> list[QAPromptVersion]:
    """Recorded versions, the shipped one, and hashes seen only on runs with
    a golden trial, oldest first."""
    shipped = shipped_prompt_sha256(stage)
    runs_by_sha: dict[str, list[QAEvalRunSummary]] = defaultdict(list)
    for run in runs:
        if run.with_golden:
            runs_by_sha[run.prompt_sha256].append(run)
    versions = []
    for sha in rows.keys() | runs_by_sha.keys() | {shipped}:
        row = rows.get(sha)
        sha_runs = runs_by_sha.get(sha, [])
        first = sha_runs[0] if sha_runs else None
        if row is not None:
            author = row.git_author or row.created_by_user_id
            first_seen_at = row.git_committed_at or row.created_at
        else:
            author = owners[first.experiment_id] if first else None
            # The shipped prompt with no row and no run is new with this
            # deployment.
            first_seen_at = first.created_at if first else utcnow()
        versions.append(
            QAPromptVersion(
                sha256=sha,
                name=first.title if first else _title(names, sha, ""),
                author=author,
                first_seen_at=first_seen_at,
                git_commit=row.git_commit if row is not None else None,
                text_available=row is not None or sha == shipped,
                is_production=sha == shipped,
                previous_sha256=None,
                runs=len(sha_runs),
                # Subset runs score picked trials only, so the latest full
                # run stands for the version.
                latest_run_id=next(
                    (r.experiment_id for r in reversed(sha_runs) if not r.subset),
                    None,
                ),
            )
        )
    versions.sort(key=lambda version: (version.first_seen_at, version.sha256))
    previous = None
    for version in versions:
        version.previous_sha256 = previous
        if version.text_available:
            previous = version.sha256
    return versions


async def qa_eval_status_core(
    session: AsyncSession, *, org_id: str | None, stage: QAStage
) -> QAEvalStatusResponse:
    spec = STAGES[stage]
    goldens = await _goldens(session, org_id, stage)
    rows = await _prompt_rows(session, org_id, stage, None)
    names = _names(stage, rows)
    replays = await _replays(session, org_id, stage=stage) if spec.replayable else []
    runs = _score_runs(replays, stage, _counted(goldens), names)
    summaries = [run.summary for run in runs]
    charted = [run for run in summaries if run.charted]
    shipped = shipped_prompt_sha256(stage)
    costs = [
        case.cost_usd for run in runs for case in run.cases if case.cost_usd is not None
    ]
    states = Counter(state for _, _, state in goldens)
    labels = Counter(
        spec.label(golden.expected)
        for _, golden, state in goldens
        if state == "counted"
    )
    spend_by_model: dict[str, float] = defaultdict(float)
    for run in summaries:
        spend_by_model[run.model_name] += run.cost_usd
    return QAEvalStatusResponse(
        replayable=spec.replayable,
        production_prompt_sha256=shipped,
        goldens=QAGoldensSummary(
            counted=states["counted"],
            waiting=states["waiting"],
            stale=states["stale"],
            labels=[
                QALabelCount(label=label, count=count)
                for label, count in labels.most_common()
            ],
        ),
        cost_per_trial=sum(costs) / len(costs) if costs else None,
        spend=QAEvalSpend(
            total_usd=sum(run.cost_usd for run in summaries),
            trials=sum(run.replays for run in summaries),
            cost_missing=sum(run.cost_missing for run in summaries),
            by_model=[
                QAEvalModelSpend(model_name=name, cost_usd=cost)
                for name, cost in sorted(
                    spend_by_model.items(), key=lambda item: -item[1]
                )
            ],
        ),
        runs=summaries,
        production_run_id=next(
            (
                run.experiment_id
                for run in reversed(charted)
                if run.prompt_sha256 == shipped
            ),
            None,
        ),
        frontier=_frontier(charted),
        models=_models(charted),
        versions=_versions(
            stage,
            rows,
            names,
            summaries,
            {replay.experiment_id: replay.owner_user_id for replay in replays},
        ),
    )
