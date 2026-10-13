"""Upgrade an Oddish CLI install: install.sh via the Oddish API, or PyPI via uv/pip."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Annotated, NoReturn

import typer

from oddish.cli._package import (
    InstallInfo,
    PackageError,
    fetch_api_latest,
    fetch_pypi_latest,
    install_script_command,
    inspect_install,
    is_outdated,
    query_installed_version,
    upgrade_command,
)
from oddish.cli.config import console, error_console, get_api_key, get_api_url


def update_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report whether a newer PyPI release is available."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the upgrade command without running it.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reinstall even when the PyPI version already matches.")
    ] = False,
) -> None:
    """Upgrade the CLI. Script installs need ODDISH_API_KEY; Homebrew uses `brew upgrade`."""
    info = inspect_install()
    env: dict[str, str] | None = None
    if info.source == "homebrew":
        _fail("This install is managed by Homebrew. Run `brew upgrade abundant-ai/tap/oddish`.", json_output=json_output, info=info)
    if info.source == "script":
        # The install script always fetches the newest release, so there is
        # nothing to pin; it reads the key and API URL from its environment.
        api_url, api_key = get_api_url(), get_api_key()
        command = install_script_command(api_url)
        if not api_key:
            _fail("Set ODDISH_API_KEY to update (Oddish Settings > API keys).", json_output=json_output, info=info, command=command)
        env = {**os.environ, "ODDISH_API_KEY": api_key, "ODDISH_API_URL": api_url}
        try:
            latest = fetch_api_latest(api_url, api_key)
        except PackageError as exc:
            _fail(str(exc), json_output=json_output, info=info, command=command)
        already_latest = not is_outdated(info.version, latest)
    else:
        try:
            latest = fetch_pypi_latest()
            pypi_error = None
        except PackageError as exc:
            latest = None
            pypi_error = exc
        already_latest = latest is not None and not is_outdated(info.version, latest)
        # Pin the installed version for same-version --force reinstalls. When
        # PyPI metadata is unavailable, also pin so --force rebuilds the current
        # release instead of emitting a bare unpinned upgrade.
        pin_version = (
            info.version if force and (already_latest or latest is None) else None
        )
        try:
            command = upgrade_command(info, force=force, pin_version=pin_version)
        except PackageError as exc:
            _fail(str(exc), json_output=json_output, info=info)
        if pypi_error is not None and not force:
            _fail(str(pypi_error), json_output=json_output, info=info, command=command)

    payload = {
        **info.as_dict(),
        "command": command,
        "latest": latest,
        "update_available": None if latest is None else not already_latest,
    }

    if check:
        payload["action"] = "check"
        _emit(payload, json_output=json_output, info=info, latest=latest)
        if latest is not None and not already_latest:
            raise typer.Exit(1)
        return

    if already_latest and not force:
        payload["action"] = "already_latest"
        _emit(payload, json_output=json_output, info=info, latest=latest)
        return

    if dry_run:
        payload["action"] = "dry_run"
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(subprocess.list2cmdline(command))
        return

    if not json_output:
        via = info.manager_label
        if latest:
            console.print(f"Updating oddish {info.version} → {latest} via {via}")
        else:
            console.print(f"Updating oddish {info.version} via {via}")

    completed = subprocess.run(
        command, check=False, stdout=sys.stderr if json_output else None, env=env
    )
    if completed.returncode != 0:
        _fail(
            f"Upgrade command exited {completed.returncode}.",
            json_output=json_output,
            info=info,
            command=command,
            code=completed.returncode,
        )

    try:
        new_version = query_installed_version(sys.executable)
    except PackageError:
        new_version = None
    payload["action"] = "updated"
    payload["new_version"] = new_version
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    if new_version:
        console.print(f"[green]Updated[/green] to oddish {new_version}")
    else:
        console.print("[green]Update finished[/green]")


def _emit(
    payload: dict[str, object], *, json_output: bool, info: InstallInfo, latest: str | None
) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"oddish {info.version}")
    typer.echo(f"Source: {info.source_label} ({info.manager_label})")
    if latest is None:
        return
    if payload.get("action") == "already_latest":
        typer.echo(f"Latest: {latest} (already up to date)")
    elif is_outdated(info.version, latest):
        typer.echo(f"Latest: {latest} (update available)")
    else:
        typer.echo(f"Latest: {latest}")


def _fail(
    message: str,
    *,
    json_output: bool,
    info: InstallInfo,
    command: list[str] | None = None,
    code: int = 1,
) -> NoReturn:
    if json_output:
        typer.echo(
            json.dumps(
                {**info.as_dict(), "action": "error", "error": message, "command": command},
                indent=2,
            )
        )
    else:
        error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)
