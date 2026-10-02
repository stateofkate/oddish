"""Prepare playback from the files of a completed trial."""

from typing import Annotated

import httpx
import typer

from oddish.cli.config import error_console, get_api_url, get_auth_headers, print_json

playback_app = typer.Typer(
    help="Prepare recorded trial playback.", no_args_is_help=True
)


@playback_app.command()
def rebuild(
    trial_id: Annotated[str, typer.Argument(help="Completed trial ID.")],
    api: Annotated[str | None, typer.Option("--api", help="Oddish API URL.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output playback status as JSON.")
    ] = False,
) -> None:
    """Rebuild playback from saved files without rerunning the trial."""
    api_url = (api or get_api_url()).rstrip("/")
    try:
        response = httpx.post(
            f"{api_url}/trials/{trial_id}/playback/rebuild",
            headers=get_auth_headers(api_url),
            timeout=300.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        error_console.print(
            f"Playback rebuild failed (HTTP {exc.response.status_code}): {exc.response.text}",
            markup=False,
        )
        raise typer.Exit(1) from exc
    except httpx.RequestError as exc:
        error_console.print(f"Playback rebuild request failed: {exc}", markup=False)
        raise typer.Exit(1) from exc
    playback = response.json()
    if json_output:
        print_json(playback)
    else:
        typer.echo(f"Playback: {playback['index_status']}")
        if playback.get("index_error"):
            typer.echo(f"Reason: {playback['index_error']}")
    if playback["index_status"] == "invalid":
        raise typer.Exit(1)
