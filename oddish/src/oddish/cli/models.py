"""``oddish models`` — configured models, their concurrency limits, and health."""

from __future__ import annotations

from typing import Annotated, NoReturn

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from oddish.cli.config import error_console, get_api_url, get_auth_headers, print_json

console = Console()

_HEALTH_STYLES = {"healthy": "green", "failing": "red", "unchecked": "dim"}


def _fail(message: str) -> NoReturn:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(1)


def models(
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    """List configured models with their concurrency limits, load, and health.

    Pass a row's model to `oddish run -m` (or a sweep's model_name). Healthy
    models passed the endpoint monitor's latest probe.
    """
    api_url = (api_url or get_api_url()).rstrip("/")
    try:
        with httpx.Client(timeout=60.0, headers=get_auth_headers(api_url)) as client:
            response = client.get(f"{api_url}/models")
    except httpx.RequestError as exc:
        _fail(f"Failed to connect to API: {exc}")
    if response.status_code == 403:
        _fail("The model list is available only to Abundant organization keys.")
    if response.status_code == 404:
        _fail("This server does not list models yet; it needs a newer Oddish server.")
    if response.status_code != 200:
        try:
            detail = response.json().get("detail") or response.text
        except Exception:  # noqa: BLE001
            detail = response.text
        _fail(f"{response.status_code} - {detail}")
    payload = response.json()
    if json_output:
        print_json(payload)
        return

    table = Table(title="Configured models")
    table.add_column("Model", no_wrap=True)
    table.add_column("Provider")
    for column in ("Limit", "Running", "Queued"):
        table.add_column(column, justify="right")
    table.add_column("Health")
    for row in payload["models"]:
        health = row["health"]
        table.add_row(
            escape(row["model"]),
            escape(row.get("provider") or "-"),
            str(row["limit"]),
            str(row["running"]),
            str(row["queued"]),
            f"[{_HEALTH_STYLES.get(health, 'dim')}]{escape(health)}[/]",
        )
    console.print(table)
