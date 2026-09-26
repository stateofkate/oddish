"""``oddish qa evals``: the QA Evals page's runs view from the terminal. Each
command prints the link to the same view."""

from __future__ import annotations

import difflib
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, get_args
from uuid import uuid4

import httpx
import typer

from oddish.analyze.qa_stages import QAStage
from oddish.cli.qa_api import (
    ApiOption,
    GroupOption,
    JsonOption,
    QAClient,
    QAEvalGroup,
    StageOption,
    emit,
    qa_client,
)

evals_app = typer.Typer(
    help="Score QA prompt versions against the goldens.", no_args_is_help=True
)

_GROUPS: tuple[str, ...] = get_args(QAEvalGroup)


def _pct(accuracy: float | None) -> str:
    return "—" if accuracy is None else f"{round(accuracy * 100)}%"


def _score(run: dict[str, Any]) -> str:
    if not run["with_golden"]:
        return "no golden trials"
    score = f"{_pct(run['accuracy'])} ({run['correct']}/{run['judged']})"
    return f"{score} subset" if run["subset"] else score


def _status(client: QAClient, stage: QAStage) -> dict[str, Any]:
    return client.call("GET", "/qa-evals/status", params={"stage": stage.value})


def _version(status: dict[str, Any], prefix: str, hint: str) -> dict[str, Any]:
    """The one version whose hash starts with ``prefix``, so the short hashes
    printed here can be passed back. Only a version with recorded text can
    be replayed or diffed."""
    matches = [v for v in status["versions"] if v["sha256"].startswith(prefix)]
    if len(matches) != 1:
        raise typer.BadParameter(
            f"{prefix!r} matches {len(matches)} prompt versions", param_hint=hint
        )
    if not matches[0]["text_available"]:
        raise typer.BadParameter(f"{prefix!r} has no recorded text", param_hint=hint)
    return matches[0]


@evals_app.command("status")
def status(
    stage: StageOption = QAStage.POST_TRIAL,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """The goldens, every run newest first, each model's latest run, and
    spend. ``frontier``: no cheaper run is as accurate."""
    with qa_client(api) as client:
        data = _status(client, stage)
        url = client.page_url(stage=stage)

    def lines():
        goldens, spend = data["goldens"], data["spend"]
        labels = ", ".join(f"{g['label']} {g['count']}" for g in goldens["labels"])
        yield (
            f"goldens  {goldens['counted']} ({labels or 'none'}), "
            f"{goldens['waiting']} waiting for review, {goldens['stale']} stale"
        )
        runs = {run["experiment_id"]: run for run in data["runs"]}
        production = runs.get(data["production_run_id"] or "")
        yield (
            f"production prompt {data['production_prompt_sha256'][:8]}: "
            + (_score(production) if production else "no full run yet")
        )
        frontier = set(data["frontier"])
        for run in reversed(data["runs"]):
            yield (
                f"{run['experiment_id']}  {run['created_at'][:10]}  "
                f"{run['prompt_sha256'][:8]} {run['title']}  "
                f"{run['model_name']}  {_score(run)}  {run['errored']} errored  "
                + (f"{run['pending']} running  " if run["pending"] else "")
                + f"{run['replays']} trials  ${run['cost_usd']:.2f}"
                + ("  frontier" if run["experiment_id"] in frontier else "")
            )
        for row in data["models"]:
            latest = runs[row["latest_run_id"]]
            per_trial = latest["cost_per_trial"]
            yield (
                f"model {row['model_name']}  {_score(latest)}  "
                + (f"${per_trial:.2f}/trial  " if per_trial is not None else "")
                + f"{row['runs']} runs  latest {row['latest_run_id']}"
            )
        by_model = ", ".join(
            f"{m['model_name']} ${m['cost_usd']:.2f}" for m in spend["by_model"]
        )
        yield (
            f"spend  ${spend['total_usd']:.2f} over {spend['trials']} trials"
            + (f" ({by_model})" if by_model else "")
            + (
                f", {spend['cost_missing']} missing a cost"
                if spend["cost_missing"]
                else ""
            )
        )

    emit(json_output, data, lines(), url)


def _cases(
    client: QAClient, run_id: str, only: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A run and its cases in the ``only`` group (all when None)."""
    data = client.call("GET", f"/qa-evals/runs/{run_id}")
    return data, [case for case in data["cases"] if only in (None, case["group"])]


@evals_app.command("run")
def run(
    run_id: Annotated[str, typer.Argument(help="The replay experiment id.")],
    only: GroupOption = None,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """One run case by case. Every case is in one group, and the group
    counts add up to the run's cases."""
    with qa_client(api) as client:
        data, cases = _cases(client, run_id, only)
        url = client.page_url(run=run_id, filter=only)

    def lines():
        summary = data["summary"]
        yield (
            f"{summary['experiment_name']}: {_score(summary)}, "
            f"{summary['errored']} errored, "
            f"{summary['pending']} running, ${summary['cost_usd']:.2f}"
            + (
                f" ({summary['cost_missing']} missing a cost)"
                if summary["cost_missing"]
                else ""
            )
        )
        counts = Counter(case["group"] for case in data["cases"])
        yield (
            " · ".join(f"{group} {counts[group]}" for group in _GROUPS if counts[group])
            + f" = {len(data['cases'])} cases"
        )
        for case in sorted(cases, key=lambda case: _GROUPS.index(case["group"])):
            yield (
                f"{case['group']}  {case['original_trial_id']}  expected "
                f"{case['expected'] or '—'}  got {case['got'] or case['outcome']}"
            )
        for group in data["error_groups"]:
            yield f"{group['count']} × {group['message']}"

    emit(json_output, {**data, "cases": cases}, lines(), url)


@evals_app.command("versions")
def versions(
    stage: StageOption = QAStage.POST_TRIAL,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Every prompt version of the stage, newest first. The link shows the
    newest."""
    with qa_client(api) as client:
        data = _status(client, stage)
        newest = data["versions"][-1]["sha256"] if data["versions"] else None
        url = client.page_url(stage=stage, version=newest)
    runs = {run["experiment_id"]: run for run in data["runs"]}

    def lines():
        for version in reversed(data["versions"]):
            latest = runs.get(version["latest_run_id"] or "")
            yield (
                f"{version['sha256'][:8]}  {version['first_seen_at'][:10]}  "
                f"{version['runs']} runs  {_score(latest) if latest else '—'}  "
                f"{version['name']}"
                + (" production" if version["is_production"] else "")
                + ("" if version["text_available"] else " (no text)")
            )

    emit(json_output, data["versions"], lines(), url)


@evals_app.command("diff")
def diff(
    sha256: Annotated[str, typer.Argument(help="Prompt version (sha256 or prefix).")],
    base: Annotated[
        str | None,
        typer.Option(help="Version to diff against. Omitted: the previous version."),
    ] = None,
    stage: StageOption = QAStage.POST_TRIAL,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """A unified diff of one prompt version against another."""
    params = {"stage": stage.value}
    with qa_client(api) as client:
        status_data = _status(client, stage)
        version = _version(status_data, sha256, "SHA256")
        base_sha = (
            _version(status_data, base, "--base")["sha256"]
            if base
            else version["previous_sha256"]
        )
        after = client.call(
            "GET", f"/qa-evals/versions/{version['sha256']}", params=params
        )
        before = (
            client.call("GET", f"/qa-evals/versions/{base_sha}", params=params)
            if base_sha
            else {"sha256": "", "text": ""}
        )
        # The page's default base is the previous version, so it stays out
        # of the link.
        url = client.page_url(
            stage=stage,
            version=after["sha256"],
            base_version=base_sha if base else None,
        )
    unified = "".join(
        difflib.unified_diff(
            before["text"].splitlines(keepends=True),
            after["text"].splitlines(keepends=True),
            fromfile=f"prompt@{before['sha256'][:8] or 'empty'}",
            tofile=f"prompt@{after['sha256'][:8]}",
        )
    )
    emit(
        json_output,
        {"sha256": after["sha256"], "base_sha256": base_sha, "diff": unified},
        unified.splitlines(),
        url,
    )


@evals_app.command("start")
def start(
    prompt: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="Prompt text file to test."),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option(help="Recorded prompt version (sha256 or prefix) instead."),
    ] = None,
    trial: Annotated[
        list[str] | None,
        typer.Option(help="Replay only this solver trial (repeatable)."),
    ] = None,
    rerun: Annotated[
        str | None,
        typer.Option(
            help="Replay only the golden trials of this run's --filter group."
        ),
    ] = None,
    only: GroupOption = None,
    name: Annotated[str | None, typer.Option(help="Run name.")] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            help="Reuse this submission key and the same options when retrying."
        ),
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="QA model. Omitted: the production QA model.")
    ] = None,
    stage: StageOption = QAStage.POST_TRIAL,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Start without asking.")
    ] = False,
    api: ApiOption = None,
    json_output: JsonOption = False,
) -> None:
    """Replay the goldens' trials with one prompt version: all of them, or
    only picked ones (a subset run, left out of the charts). Asks first,
    with a cost estimate.

    oddish qa evals start --prompt classify_prompt.txt
    oddish qa evals start --version SHA --rerun RUN --filter wrong
    """
    if (prompt is None) == (version is None):
        raise typer.BadParameter("give exactly one of --prompt and --version")
    if only and not rerun:
        raise typer.BadParameter("--filter needs --rerun")
    body: dict[str, Any] = {"stage": stage.value, "model": model}
    if prompt is not None:
        body |= {"prompt_name": prompt.stem, "prompt_text": prompt.read_text()}
    with qa_client(api) as client:
        status_data = _status(client, stage)
        if not status_data["replayable"]:
            raise typer.BadParameter("cannot be replayed yet", param_hint="--stage")
        if version is not None:
            body["prompt_sha256"] = _version(status_data, version, "--version")[
                "sha256"
            ]
        picked = list(trial or [])
        if rerun:
            # As on the page, only cases with a golden: the rest cannot score.
            _, cases = _cases(client, rerun, only)
            picked += [case["source_trial_id"] for case in cases if case["golden_id"]]
        picked = list(dict.fromkeys(picked))
        if (trial or rerun) and not picked:
            raise typer.BadParameter("no trials picked", param_hint="--rerun")
        count = len(picked) or status_data["goldens"]["counted"]
        per_trial = status_data["cost_per_trial"]
        estimate = (
            f"about ${per_trial * count:.2f} at ${per_trial:.2f}/trial"
            if per_trial is not None
            else "no past runs to estimate cost from"
        )
        what = f"{count} picked trials" if picked else f"all {count} golden trials"
        if not yes and not typer.confirm(f"Replay {what} ({estimate})?", err=True):
            raise typer.Exit(1)
        if picked:
            body["source_trial_ids"] = picked
        else:
            body["goldens"] = True
        key = idempotency_key or str(uuid4())
        body["name"] = name or f"qa-eval-{key[:32]}"
        try:
            created = client.call(
                "POST", "/qa-evals", json=body, headers={"Idempotency-Key": key}
            )
        except (httpx.TransportError, typer.Exit) as exc:
            typer.echo(
                f"Retry with --idempotency-key {key!r} and the same options.", err=True
            )
            raise typer.Exit(1) from exc
        url = client.page_url(stage=stage, run=created["experiment_id"])
    for line in created["skipped"]:
        typer.echo(f"skipped {line}", err=True)
    emit(
        json_output,
        created,
        [
            f"started {created['experiment_id']}: {len(created['trials'])} trials, "
            f"prompt {created['prompt_sha256'][:8]}, model {created['model']}"
        ],
        url,
    )
