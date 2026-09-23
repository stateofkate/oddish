"""``oddish costs`` — billable-spend accounting without direct DB access.

Wraps the admin cost endpoints:

- ``GET /admin/costs``              -> org-wide breakdown (by user, by model)
- ``GET /admin/costs/users/{id}``  -> one user's billed spend (by task, by model)

Admin-gated on hosted Oddish (a full-scope API key); a self-hosted core server
does not expose these routes.
"""

from __future__ import annotations

import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli._collection_refs import _resolve_task_id
from oddish.cli.config import get_api_url, get_auth_headers, print_json, require_api_key

console = Console()
error_console = Console(stderr=True)


def _fmt_usd(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _window_label(window_days: int) -> str:
    return "all time" if window_days == 0 else f"last {window_days}d"


def _render_org_costs(data: dict[str, Any]) -> None:
    totals = data.get("totals") or {}
    console.print(
        f"[bold]Total spend:[/bold] {_fmt_usd(totals.get('cost_usd'))} "
        f"across {totals.get('trial_count', 0)} trials, "
        f"{totals.get('user_count', 0)} users, "
        f"{totals.get('experiment_count', 0)} experiments"
    )
    qa = totals.get("qa_cost_usd")
    compute = totals.get("compute_cost_usd")
    verifier = totals.get("verifier_cost_usd")
    extras = []
    if qa:
        extras.append(f"QA {_fmt_usd(qa)}")
    if compute:
        extras.append(f"compute {_fmt_usd(compute)}")
    if verifier:
        extras.append(f"verifier {_fmt_usd(verifier)}")
    if extras:
        console.print(f"[dim]Also: {', '.join(extras)}[/dim]")
    month_budget = totals.get("month_budget_usd")
    if month_budget is not None:
        console.print(
            f"[bold]This month:[/bold] {_fmt_usd(totals.get('month_cost_usd'))} "
            f"/ {_fmt_usd(month_budget)} budget"
        )

    by_user = data.get("by_user") or []
    if by_user:
        table = Table(title="By user", show_header=True)
        table.add_column("User", style="cyan")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in by_user[:15]:
            who = row.get("name") or row.get("email") or row.get("label") or row.get("key")
            table.add_row(str(who), _fmt_usd(row.get("cost_usd")), str(row.get("trial_count", 0)))
        console.print(table)

    by_model = data.get("by_model") or []
    if by_model:
        table = Table(title="By model", show_header=True)
        table.add_column("Model", style="cyan")
        table.add_column("Provider")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in by_model[:15]:
            table.add_row(
                str(row.get("model", "-")),
                str(row.get("provider", "-")),
                _fmt_usd(row.get("cost_usd")),
                str(row.get("trial_count", 0)),
            )
        console.print(table)


def _render_user_costs(data: dict[str, Any]) -> None:
    who = data.get("name") or data.get("email") or data.get("billed_user_id")
    console.print(f"[bold]User:[/bold] {who}")
    if data.get("email"):
        console.print(f"[bold]Email:[/bold] {data['email']}")
    totals = data.get("totals") or {}
    console.print(
        f"[bold]Billed spend:[/bold] {_fmt_usd(totals.get('cost_usd'))} "
        f"across {totals.get('trial_count', 0)} trials, "
        f"{totals.get('task_count', 0)} tasks"
    )

    tasks = data.get("tasks") or []
    if tasks:
        table = Table(title="By task", show_header=True)
        table.add_column("Task", style="cyan")
        table.add_column("Name")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in tasks[:20]:
            table.add_row(
                str(row.get("task_id", "-")),
                (row.get("task_name") or "-")[:40],
                _fmt_usd(row.get("cost_usd")),
                str(row.get("trial_count", 0)),
            )
        console.print(table)

    # By-model rollup, aggregated from each task's per-model breakdown (the
    # user payload carries models per task, not as a top-level list). Mirrors
    # the by-model table the org-wide view prints.
    by_model: dict[tuple[str, str], dict[str, float]] = {}
    for task in tasks:
        for model_row in task.get("models") or []:
            key = (str(model_row.get("model", "-")), str(model_row.get("provider", "-")))
            bucket = by_model.setdefault(key, {"cost_usd": 0.0, "trial_count": 0})
            bucket["cost_usd"] += float(model_row.get("cost_usd") or 0.0)
            bucket["trial_count"] += int(model_row.get("trial_count") or 0)
    if by_model:
        table = Table(title="By model", show_header=True)
        table.add_column("Model", style="cyan")
        table.add_column("Provider")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for (model, provider), bucket in sorted(
            by_model.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        ):
            table.add_row(
                model,
                provider,
                _fmt_usd(bucket["cost_usd"]),
                str(int(bucket["trial_count"])),
            )
        console.print(table)


# Analysis trial kinds with their own column; other non-agent kinds count as QA.
_ANALYSIS_COLUMNS = {"qa": "qa_usd", "audit": "audit_usd", "summarize": "summary_usd"}
_COLUMNS = {
    "solver_usd": "Solver",
    "qa_usd": "QA",
    "audit_usd": "Audit",
    "summary_usd": "Summaries",
    "judge_other_usd": "Judge & other",
    "verifier_usd": "Verifier",
}
_REVIEW = ("qa_usd", "audit_usd", "summary_usd", "judge_other_usd")


def _get(client: httpx.Client, url: str, **params: Any) -> Any:
    response = client.get(url, params=params)
    if response.status_code != 200:
        error_console.print(
            f"[red]Failed to get {url} (HTTP {response.status_code}):[/red] "
            f"{response.text}"
        )
        raise typer.Exit(1)
    return response.json()


def _solver_usd(trials: list[dict[str, Any]]) -> float:
    """Agent runs Oddish executed; probes and imported runs are not solver spend."""
    return sum(
        float(t.get("cost_usd") or 0.0)
        for t in trials
        if (t.get("kind") or "agent") == "agent"
        and not t.get("is_probe")
        and t.get("origin") != "imported"
    )


def _task_row(
    task_id: str,
    detail: dict[str, Any],
    experiment_id: str | None = None,
    listed_trial_ids: set | None = None,
) -> dict[str, Any]:
    """One task's spend by type from ``GET /tasks/{id}/detail``.

    Review columns come from the task's analysis trials; ``qa_cost_usd`` also
    holds judge charges and older ledger rows, reported as the remainder.
    """
    task = detail.get("task") or {}
    totals = detail.get("totals") or {}
    trials = task.get("trials") or []
    row: dict[str, Any] = {"task_id": task_id, "task_name": task.get("name")}
    row.update(dict.fromkeys(_COLUMNS, 0.0))
    if experiment_id is not None:
        in_experiment = listed_trial_ids or set()
        solver_trials = [
            t
            for t in trials
            if t.get("experiment_id") == experiment_id or t.get("id") in in_experiment
        ]
    else:
        solver_trials = trials
    row["solver_usd"] = _solver_usd(solver_trials)
    for trial in trials:
        kind = trial.get("kind") or "agent"
        if kind != "agent":
            row[_ANALYSIS_COLUMNS.get(kind, "qa_usd")] += float(trial.get("cost_usd") or 0.0)
    analysis = row["qa_usd"] + row["audit_usd"] + row["summary_usd"]
    row["judge_other_usd"] = max(float(totals.get("qa_cost_usd") or 0.0) - analysis, 0.0)
    row["verifier_usd"] = float(totals.get("verifier_cost_usd") or 0.0)
    row["total_usd"] = sum(row[k] for k in _COLUMNS)
    return row


def _experiment_trial_ids(client, api_url: str, experiment_id: str) -> dict[str, set]:
    """Task id -> trial ids the experiment lists, which include gathered runs.

    The listing shows one version per task, so it only finds the tasks and the
    trials gathered from elsewhere; ``_task_row`` takes every version's runs
    from task detail.
    """
    listed: dict[str, set] = {}
    offset = 0
    while True:
        page = _get(
            client,
            f"{api_url}/tasks",
            experiment_id=experiment_id,
            include_trials="true",
            compact_trials="true",
            limit=500,
            offset=offset,
        )
        for task in page:
            listed[task["id"]] = {t.get("id") for t in task.get("trials") or []}
        if len(page) < 500:
            return listed
        offset += 500


def _render_task_costs(rows: list[dict[str, Any]], scope: str) -> None:
    shown = [k for k in _COLUMNS if any(r[k] for r in rows)] or ["solver_usd"]
    table = Table(title=f"Cost by task ({scope})", show_header=True)
    table.add_column("Task", style="cyan", overflow="fold")
    for key in shown:
        table.add_column(_COLUMNS[key], justify="right", no_wrap=True)
    table.add_column("Total", justify="right", no_wrap=True, style="bold")
    table.add_column("QA share", justify="right", no_wrap=True)
    for row in rows:
        total = row["total_usd"]
        review = sum(row[k] for k in _REVIEW)
        table.add_row(
            row["task_name"] or row["task_id"],
            *[_fmt_usd(row[k]) for k in shown],
            _fmt_usd(total),
            f"{review / total:.0%}" if total else "-",
        )
    console.print(table)
    sums = {k: sum(r[k] for r in rows) for k in shown}
    parts = ", ".join(f"{_COLUMNS[k]} {_fmt_usd(v)}" for k, v in sums.items())
    console.print(
        f"[bold]Total:[/bold] {_fmt_usd(sum(r['total_usd'] for r in rows))} "
        f"({parts}) across {len(rows)} tasks"
    )


def _task_costs(
    api_url: str,
    *,
    tasks: list[str],
    experiment: str | None,
    json_output: bool,
    csv_output: bool,
) -> None:
    try:
        with httpx.Client(timeout=60.0, headers=get_auth_headers(api_url)) as client:
            listed: dict[str, set] = {}
            if experiment:
                listed = _experiment_trial_ids(client, api_url, experiment)
                task_ids = list(listed)
                scope = "solver: this experiment; review and verifier: whole task"
            else:
                task_ids = []
                for ref in tasks:
                    task_id = _resolve_task_id(client, api_url, ref)
                    if task_id is None:
                        error_console.print(f"[red]Task not found: {ref}[/red]")
                        raise typer.Exit(1)
                    task_ids.append(task_id)
                scope = "all versions"
            headers = get_auth_headers(api_url)

            def _detail(tid: str) -> Any:
                with httpx.Client(timeout=60.0, headers=headers) as worker:
                    return _get(worker, f"{api_url}/tasks/{tid}/detail")

            with ThreadPoolExecutor(max_workers=8) as pool:
                details = list(pool.map(_detail, task_ids))
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    rows = [
        _task_row(tid, detail, experiment, listed.get(tid))
        for tid, detail in zip(task_ids, details)
    ]
    rows.sort(key=lambda r: r["total_usd"], reverse=True)
    if json_output:
        print_json({"scope": scope, "tasks": rows})
    elif csv_output:
        writer = csv.DictWriter(
            sys.stdout, fieldnames=["task_id", "task_name", *_COLUMNS, "total_usd"]
        )
        writer.writeheader()
        writer.writerows(rows)
    else:
        _render_task_costs(rows, scope)


def costs(
    user: Annotated[
        Optional[str],
        typer.Option(
            "--user",
            help="Show one user's billed spend (by id) instead of the org-wide breakdown.",
        ),
    ] = None,
    window_days: Annotated[
        int,
        typer.Option(
            "--window-days",
            min=0,
            max=3650,
            help="Trailing window in days; 0 = all-time.",
        ),
    ] = 7,
    task: Annotated[
        Optional[list[str]],
        typer.Option(
            "--task", "-t", help="Per-task cost by type (solver, QA, audit, ...); repeatable."
        ),
    ] = None,
    experiment: Annotated[
        Optional[str],
        typer.Option(
            "--experiment", "-e", help="Per-task cost by type for an experiment (id)."
        ),
    ] = None,
    csv_output: Annotated[
        bool, typer.Option("--csv", help="With --task/--experiment, write CSV.")
    ] = False,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the raw cost breakdown JSON."),
    ] = False,
) -> None:
    """Show spend: org-wide, per user, or per task split into solver and QA.

    --task and --experiment need only a read key. The org-wide and --user
    views need a full-scope key on hosted Oddish.
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if task or experiment:
        if task and experiment:
            error_console.print("[red]Pass --task or --experiment, not both.[/red]")
            raise typer.Exit(1)
        if user:
            error_console.print(
                "[red]Pass --user or --task/--experiment, not both.[/red]"
            )
            raise typer.Exit(1)
        _task_costs(
            api_url,
            tasks=list(task or []),
            experiment=experiment,
            json_output=json_output,
            csv_output=csv_output,
        )
        return

    if user:
        path = f"/admin/costs/users/{user}"
    else:
        path = "/admin/costs"

    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.get(
                f"{api_url}{path}", params={"window_days": window_days}
            )
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code in (401, 403):
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(
                f"[red]Cost accounting requires a full-scope API key "
                f"(got HTTP {response.status_code}).[/red]"
            )
        raise typer.Exit(1)
    if response.status_code == 404:
        if json_output:
            print_json({"error": response.text, "status": 404})
        elif user:
            # Hosted returns "User not found"; a core server has no route at all.
            error_console.print(
                f"[red]No cost data for user '{user}' (HTTP 404): the user was "
                "not found, or cost accounting is only available on hosted "
                "Oddish.[/red]"
            )
        else:
            error_console.print(
                "[red]Cost accounting is only available on hosted Oddish.[/red]"
            )
        raise typer.Exit(1)
    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(f"[red]Failed to get costs:[/red] {response.text}")
        raise typer.Exit(1)

    data = response.json()
    if json_output:
        print_json(data)
        return

    console.print(f"[dim]Window: {_window_label(window_days)}[/dim]")
    if user:
        _render_user_costs(data)
    else:
        _render_org_costs(data)
