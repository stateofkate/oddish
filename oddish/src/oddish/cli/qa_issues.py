"""``oddish qa-issues`` — QA issues by category for an experiment or delivery."""

from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, print_json
from oddish.cli.delivery import _fail, _request, _resolve_delivery_id

console = Console()


def _percent(count: int, total: int) -> str:
    return f"{round(100 * count / total)}% ({count})" if total else "-"


def qa_issues(
    experiment: Annotated[
        Optional[str], typer.Option("--experiment", "-e", help="Experiment id")
    ] = None,
    delivery: Annotated[
        Optional[str], typer.Option("--delivery", "-d", help="Delivery id or name")
    ] = None,
    category: Annotated[
        Optional[str],
        typer.Option("--category", "-c", help="Show the tasks in one category"),
    ] = None,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    """Show QA issues by category for one experiment or one delivery."""
    if (experiment is None) == (delivery is None):
        _fail("pass exactly one of --experiment or --delivery")
    api_url = api_url or get_api_url()
    params = {"category": category, "page_size": 500} if category else {}
    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        if delivery is not None:
            path = f"deliveries/{_resolve_delivery_id(client, api_url, delivery)}"
        else:
            path = f"experiments/{experiment}"
        report = _request(client, "GET", f"{api_url}/{path}/qa-issues", params=params)
    if json_output:
        print_json(report)
        return

    summary = report["summary"]
    tasks = summary["tasks"]
    console.print(f"[bold]{escape(report['scope_name'])}[/bold]")
    console.print(
        f"Tasks {tasks} · Blocked {_percent(summary['blocked'], tasks)} · "
        f"Clear {_percent(summary['clear'], tasks)} · "
        f"Waiting for QA {_percent(summary['waiting'], tasks)}"
    )
    if category is None:
        table = Table()
        table.add_column("Category")
        table.add_column("--category", no_wrap=True, style="dim")
        for column in ("Group", "Blocking", "Not blocking", "Tasks"):
            table.add_column(column)
        for row in report["categories"]:
            table.add_row(
                row["name"],
                row["slug"],
                row["dimension"],
                str(row["blocking_issues"]),
                str(row["issues"] - row["blocking_issues"]),
                str(row["tasks"]),
            )
        console.print(table)
        return
    if report["task_total"] > len(report["tasks"]):
        console.print(f"First {len(report['tasks'])} of {report['task_total']} tasks.")
    for task in report["tasks"]:
        pr = f" PR #{task['pr_number']}" if task.get("pr_number") else ""
        console.print(
            f"\n[bold]{escape(task['task_name'])}[/bold] ({task['qa_status']}, "
            f"{sum(i['blocking'] for i in task['issues'])} blocking) "
            f"{escape(task.get('author') or '')}{pr}"
        )
        for issue in task["issues"]:
            mark = "[red]blocking[/red]" if issue["blocking"] else "not blocking"
            console.print(
                f"  - {mark}, {escape(issue['found_by'])}: {escape(issue['title'])}"
            )
