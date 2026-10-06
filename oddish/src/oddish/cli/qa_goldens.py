"""``oddish qa goldens``: the QA Evals page's labeling view from the
terminal (find experiments, label their solver trials), plus importing,
listing, reviewing and retiring golden cases. Each trial has at most one
golden, and only counted goldens (reviewed, task unchanged) score runs."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

import typer

from oddish.analyze.models import Classification
from oddish.analyze.qa_stages import STAGES, QAStage
from oddish.cli.experiment import dashboard_experiments_params
from oddish.cli.qa_api import (
    ApiOption,
    JsonOption,
    QAClient,
    QAGoldenQueueGroup,
    QAGoldenReview,
    QAGoldenState,
    emit,
    qa_client,
)

goldens_app = typer.Typer(
    help="Find experiments, label solver trials and manage the QA goldens.",
    no_args_is_help=True,
)

_TRIAL_COLUMNS = ("source_trial_id", "trial_id")
_LABEL_COLUMNS = ("expected_classification", "gold_class")
_NOTE_COLUMNS = ("note", "expected_defect", "researcher_issue_summary")
_ANSWER_KEY_SCHEMA = "gdm_source_audit_answer_key_v1"


def _subject_field(stage: QAStage) -> str:
    return "source_trial_id" if STAGES[stage].subject == "trial" else "task_version_id"


def _goldens(
    client: QAClient,
    *,
    stage: QAStage | None = None,
    source: str | None = None,
    state: QAGoldenState | None = None,
    trials: list[str] | None = None,
) -> list[dict[str, Any]]:
    """``GET /qa-goldens``: live goldens, oldest first."""
    params = [("trial", trial) for trial in trials or []]
    if stage:
        params.append(("stage", stage.value))
    if source:
        params.append(("source", source))
    if state:
        params.append(("state", state))
    return client.call("GET", "/qa-goldens", params=params)


def _label(golden: dict[str, Any]) -> str:
    """A golden's label, with its state unless it counts."""
    expected = golden["expected"]
    label = expected.get("classification") or (
        f"{len(expected['required_findings'])} findings"
    )
    return label if golden["state"] == "counted" else f"{label} {golden['state']}"


def _drawer_url(client: QAClient, trial_id: str) -> str:
    """The trial's drawer, where the page shows its label."""
    task_id = client.call("GET", f"/trials/{trial_id}")["task_id"]
    return client.trial_url(task_id, trial_id)


@goldens_app.command("experiments")
def experiments(
    member: Annotated[
        str, typer.Option(help="me, all, or a member's email or GitHub handle.")
    ] = "me",
    search: Annotated[
        str | None,
        typer.Option(help="The page's experiment search, with author: and tag: words."),
    ] = None,
    limit: Annotated[
        int, typer.Option(min=1, max=100, help="How many experiments to list.")
    ] = 25,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Experiments to label trials from, most recently active first: yours
    unless --member says otherwise. Open one with
    ``oddish qa goldens trials EXPERIMENT``."""
    member = member.strip()
    if member.lower() in ("me", "all"):
        member = member.lower()
    else:
        # The page picks members by user id; another member is an author:
        # search over everyone's, which the link carries too.
        member, search = "all", " ".join(filter(None, [f"author:{member}", search]))
    params = dashboard_experiments_params(author=member, search=search, limit=limit)
    with qa_client(api) as client:
        data = client.call("GET", "/dashboard", params=params)
        url = client.page_url(label="1", member=member, search=search)
    rows = data.get("experiments") or []

    def lines():
        for e in rows:
            author = (e.get("author") or e.get("last_author") or {}).get("name") or "—"
            date = (e.get("last_created_at") or "")[:10] or "—"
            failed = (e.get("reward_total") or 0) - (e.get("reward_success") or 0)
            yield (
                f"{e['id']}  {e['name']}  {author}  {date}  "
                f"{failed} failed / {e.get('total_trials') or 0} trials"
            )

    emit(json_output, rows, lines(), url)


@goldens_app.command("trials")
def trials(
    experiment: Annotated[
        str | None, typer.Argument(help="Experiment id or name to label trials from.")
    ] = None,
    review: Annotated[
        QAGoldenReview | None,
        typer.Option(
            help="Instead: the trials of goldens waiting for review, or stale."
        ),
    ] = None,
    queue: Annotated[
        Literal[QAGoldenQueueGroup, "all"],
        typer.Option(
            help="failed or passed (no counted golden, by reward), labeled, or all."
        ),
    ] = "failed",
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """The labeling queue: an experiment's finished solver trials, or with
    --review the trials of goldens waiting for review or stale, failed first,
    with production QA's answer and each one's golden. Label one with
    ``oddish qa goldens label TRIAL CLASSIFICATION``."""
    if (experiment is None) == (review is None):
        raise typer.BadParameter(
            "give an experiment or --review, not both", param_hint="EXPERIMENT"
        )
    params = {"review": review} if review else {"experiment": experiment}
    with qa_client(api) as client:
        data = client.call("GET", "/qa-goldens/trials", params=params)
        url = client.page_url(**params, queue=queue)
    rows = [t for t in data["trials"] if queue in ("all", t["group"])]

    def lines():
        for t in rows:
            reward = "—" if t["reward"] is None else f"{t['reward']:g}"
            qa = t["qa_classification"] or "—"
            if t["qa_subtype"]:
                qa += f" ({t['qa_subtype']})"
            golden = _label(t["golden"]) if t["golden"] else "unlabeled"
            yield (
                f"{t['trial_id']}  {t['task_name']}  reward {reward}  "
                f"QA {qa}  golden {golden}"
            )
        counts = Counter(t["group"] for t in data["trials"])
        chips = [f"{g} {counts[g]}" for g in get_args(QAGoldenQueueGroup)]
        yield f"{data['title']}: {' · '.join(chips)} · all {len(data['trials'])}"

    emit(json_output, {**data, "trials": rows}, lines(), url)


@goldens_app.command("label")
def label(
    trial_id: Annotated[str, typer.Argument(help="Solver trial to label.")],
    classification: Annotated[
        Classification, typer.Argument(help="Its correct QA classification.")
    ],
    note: Annotated[
        str | None,
        typer.Option(
            help="Why this label is right. Omitted: an unchanged label keeps its note."
        ),
    ] = None,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Set a trial's correct classification. The label counts at once and
    replaces the trial's earlier one; the same label again confirms it.

        oddish qa goldens label task-1-3 BAD_FAILURE
    """
    with qa_client(api) as client:
        golden = client.call(
            "PUT",
            f"/qa-goldens/trials/{trial_id}",
            json={"classification": classification.value, "note": note},
        )
        url = _drawer_url(client, trial_id)
    line = f"{trial_id}: {_label(golden)} (golden {golden['id']})"
    emit(json_output, golden, [line], url)


@goldens_app.command("list")
def list_goldens(
    source: Annotated[
        str | None, typer.Option(help="Only goldens imported from this source.")
    ] = None,
    stage: Annotated[QAStage | None, typer.Option(help="Only this stage.")] = None,
    waiting: Annotated[
        bool, typer.Option("--waiting", help="Only goldens waiting for review.")
    ] = False,
    trial: Annotated[
        list[str] | None, typer.Option(help="Only these solver trials (repeatable).")
    ] = None,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Live goldens, oldest first. The link shows the goldens line."""
    with qa_client(api) as client:
        goldens = _goldens(
            client,
            stage=stage,
            source=source,
            state="waiting" if waiting else None,
            trials=trial,
        )
        url = client.page_url(stage=stage)

    def lines():
        for g in goldens:
            subject = g["source_trial_id"] or g["task_version_id"]
            yield f"{g['id']}  {g['source'] or '-'}  {subject}  {_label(g)}"
        yield f"{len(goldens)} goldens"

    emit(json_output, goldens, lines(), url)


def _review(client: QAClient, golden_ids: list[str]) -> list[dict[str, Any]]:
    return [
        client.call("POST", f"/qa-goldens/{golden_id}/review")
        for golden_id in dict.fromkeys(golden_ids)
    ]


@goldens_app.command("review")
def review(
    golden_ids: Annotated[
        list[str] | None, typer.Argument(help="Goldens to mark reviewed.")
    ] = None,
    all_waiting: Annotated[
        bool,
        typer.Option(
            "--all-waiting",
            help="Every golden waiting for review (of --source). Stale ones are "
            "left for someone to check against the changed task.",
        ),
    ] = False,
    source: Annotated[
        str | None, typer.Option(help="With --all-waiting: only this source.")
    ] = None,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Mark goldens reviewed, and their task's current content as the one
    labeled, so they count toward scores. One golden links to its trial's
    drawer."""
    with qa_client(api) as client:
        ids = list(golden_ids or [])
        if all_waiting:
            ids += [g["id"] for g in _goldens(client, source=source, state="waiting")]
        reviewed = _review(client, ids)
        trial_ids = [g["source_trial_id"] for g in reviewed]
        url = (
            _drawer_url(client, trial_ids[0])
            if len(trial_ids) == 1 and trial_ids[0]
            else client.page_url()
        )
    emit(json_output, reviewed, [f"reviewed {len(reviewed)}"], url)


@goldens_app.command("retire")
def retire(
    golden_ids: Annotated[list[str], typer.Argument(help="Goldens to retire.")],
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Retire goldens: they stop counting and free their trial for a new label."""
    with qa_client(api) as client:
        for golden_id in golden_ids:
            client.call("DELETE", f"/qa-goldens/{golden_id}")
        url = client.page_url()
    emit(json_output, {"retired": golden_ids}, [f"retired {len(golden_ids)}"], url)


@dataclass(frozen=True, slots=True)
class _Row:
    """One golden to import, with the file's fields for --where."""

    stage: QAStage
    subject: str
    expected: dict[str, Any]
    note: str
    fields: dict[str, str]

    def body(self, source: str) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "source": source,
            _subject_field(self.stage): self.subject,
            "expected": self.expected,
            "note": self.note,
        }


def _first(fields: dict[str, str], columns: tuple[str, ...]) -> str:
    return next(
        (fields[c].strip() for c in columns if (fields.get(c) or "").strip()), ""
    )


def _table_rows(text: str) -> list[_Row]:
    """Post-trial goldens: a trial id and a classification per row."""
    lines = text.splitlines()
    dialect = "excel-tab" if lines and "\t" in lines[0] else "excel"
    rows = []
    for fields in csv.DictReader(lines, dialect=dialect):
        trial, label = _first(fields, _TRIAL_COLUMNS), _first(fields, _LABEL_COLUMNS)
        if not trial or not label:
            raise typer.BadParameter(
                f"every row needs one of {_TRIAL_COLUMNS} and {_LABEL_COLUMNS}",
                param_hint="FILE",
            )
        rows.append(
            _Row(
                QAStage.POST_TRIAL,
                trial,
                {"classification": label},
                _first(fields, _NOTE_COLUMNS),
                fields,
            )
        )
    return rows


def _strip_local_paths(value: Any) -> Any:
    """Answer keys cite files on the labeler's laptop; keep only repo paths."""
    if isinstance(value, dict):
        return {k: _strip_local_paths(v) for k, v in value.items() if k != "path"}
    if isinstance(value, list):
        return [_strip_local_paths(v) for v in value]
    return value


def _answer_key_rows(text: str) -> list[_Row]:
    """Pre-trial goldens: a task version and its required findings per case."""
    try:
        key = json.loads(text)
    except json.JSONDecodeError:
        key = None
    if not isinstance(key, dict) or key.get("schema_version") != _ANSWER_KEY_SCHEMA:
        raise typer.BadParameter(
            f"expected schema_version {_ANSWER_KEY_SCHEMA}", param_hint="FILE"
        )
    return [
        _Row(
            QAStage.PRE_TRIAL,
            case["task_version_id"],
            {
                "task_judgment": case.get("expected_task_judgment"),
                "required_findings": _strip_local_paths(case["required_findings"]),
            },
            f"{key.get('label_version', '')} {case.get('case_id', '')}".strip(),
            {k: str(v) for k, v in case.items() if isinstance(v, (str, bool))},
        )
        for case in key.get("cases") or []
    ]


def _matches(fields: dict[str, str], conditions: list[str]) -> bool:
    return all(
        (fields.get(column.strip()) or "").strip() == value.strip()
        for column, _, value in (c.partition("=") for c in conditions)
    )


@goldens_app.command("import")
def import_goldens(
    file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="CSV/TSV with a trial id and classification per row, or a "
            f"{_ANSWER_KEY_SCHEMA} JSON answer key (pre-trial).",
        ),
    ],
    source: Annotated[
        str | None,
        typer.Option(help="Where these labels came from. Omitted: the file name."),
    ] = None,
    where: Annotated[
        list[str] | None,
        typer.Option(help="Only rows with COLUMN=VALUE (repeatable, all must match)."),
    ] = None,
    review_where: Annotated[
        list[str] | None,
        typer.Option(
            help="Review rows with COLUMN=VALUE (repeatable, all must match)."
        ),
    ] = None,
    review_all: Annotated[
        bool, typer.Option("--review-all", help="Review every imported row.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would change; write nothing.")
    ] = False,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Add each row as an unreviewed golden, then review the chosen rows. A
    row whose subject already has the same golden is not added again but is
    still reviewed when chosen and waiting, so reruns are safe; a row
    disagreeing with an existing golden is refused (exit 1)."""
    source = source or file.stem
    text = file.read_text(encoding="utf-8-sig")
    rows = _answer_key_rows(text) if file.suffix == ".json" else _table_rows(text)
    if not rows:
        raise typer.BadParameter("has no rows", param_hint="FILE")
    stage = rows[0].stage
    rows = [row for row in rows if _matches(row.fields, where or [])]
    with qa_client(api) as client:
        present = {g[_subject_field(stage)]: g for g in _goldens(client, stage=stage)}
        new_rows: dict[str, tuple[_Row, bool]] = {}
        to_review: list[str] = []
        refused: list[str] = []
        conflicting: set[str] = set()
        for row in rows:
            pick = review_all or bool(
                review_where and _matches(row.fields, review_where)
            )
            if row.subject in conflicting:
                continue
            if row.subject in new_rows:
                previous, picked = new_rows[row.subject]
                if previous.expected != row.expected:
                    # Neither label is sent: a golden written now would
                    # refuse the corrected file on the rerun.
                    del new_rows[row.subject]
                    conflicting.add(row.subject)
                    refused.append(f"{row.subject}: conflicting labels in import")
                else:
                    new_rows[row.subject] = (previous, picked or pick)
                continue
            golden = present.get(row.subject)
            if golden is None and STAGES[stage].subject == "trial":
                # A copied trial's golden is stored under its original; the
                # server applies that rule when asked for one trial.
                golden = next(
                    iter(_goldens(client, stage=stage, trials=[row.subject])), None
                )
            if golden is None:
                new_rows[row.subject] = (row, pick)
            elif golden["expected"] != row.expected:
                # Relabeling or retiring is done on purpose, never by an
                # import: the row is refused and the golden left unreviewed.
                refused.append(f"{row.subject}: already labeled {golden['expected']}")
            elif pick and golden["state"] == "waiting":
                # A stale golden is left for someone to check against the
                # changed task.
                to_review.append(golden["id"])
        typer.echo(
            f"{len(rows)} rows, {len(rows) - len(new_rows)} already labeled "
            f"({len(to_review)} of those to review, {len(refused)} differently), "
            f"{len(new_rows)} to add",
            err=True,
        )
        url = client.page_url(stage=stage)
        if dry_run:
            plan = {
                "add": list(new_rows),
                "review": to_review,
                "refused": refused,
            }
            emit(json_output, plan, [], url)
            return
        added = []
        for row, pick in new_rows.values():
            golden, error = client.send("POST", "/qa-goldens", json=row.body(source))
            if error:
                refused.append(f"{row.subject}: {error}")
                continue
            added.append(golden["id"])
            if pick:
                to_review.append(golden["id"])
        reviewed = [g["id"] for g in _review(client, to_review)]
    for line in refused:
        typer.echo(f"refused {line}", err=True)
    emit(
        json_output,
        {"added": added, "reviewed": reviewed, "refused": refused},
        [f"added {len(added)}, reviewed {len(reviewed)}, refused {len(refused)}"],
        url,
    )
    if refused:
        raise typer.Exit(1)
