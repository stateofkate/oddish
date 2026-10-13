from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    print_json,
    require_api_key,
)

console = Console()

REPLAY_AGENT = "exploit-replay"
_ACTIVE = {"pending", "queued", "running", "retrying", "paused"}
# Matches HOOK_DEADLINE in oddish/core/adversarial.py.
_HOOK_DEADLINE = timedelta(hours=1)


def _pins(trial: dict) -> dict:
    return (trial.get("harbor_config") or {}).get("adversarial") or {}


def hook_pending(trial: dict, marker: str, value: object) -> bool:
    """A settled trial whose settle hook has not run, as adversarial_unsettled
    decides it: the attack's queues its replay, the replay's records findings."""
    if trial["status"] in _ACTIVE:
        return True
    if trial["status"] not in ("success", "failed") or (
        trial.get("harbor_stage") == "cancelled"
    ):
        return False
    finished = trial.get("finished_at")
    if not finished:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(
        finished.replace("Z", "+00:00")
    )
    return _pins(trial).get(marker) != value and age <= _HOOK_DEADLINE


def attack_result(attack: dict, replay: dict | None, leaked: bool = False) -> str:
    """One word per attack, the same rules as the task page's attack card.

    ``leaked``: the replay recorded a finding, such as a copied protected file.
    """
    if attack["status"] in _ACTIVE:
        return attack["status"]
    if replay and (
        leaked or (replay["status"] == "success" and replay.get("reward") == 1)
    ):
        return "MUST FIX"
    if replay and replay["status"] in _ACTIVE:
        return "replaying"
    if attack.get("harbor_stage") == "cancelled":
        return "cancelled"
    if attack.get("reward") == 1:
        return "needs a human"
    if (
        attack["status"] != "success"
        or attack.get("reward") is None
        or attack.get("error_message")
    ):
        return "incomplete"
    return "no defect"


def _rows(
    client: httpx.Client, api_url: str, task_id: str, version: int, ids: list[str]
) -> list[dict]:
    resp = client.get(
        f"{api_url}/tasks/{task_id}/trials",
        params={"include_adversarial": "true", "version": version},
    )
    resp.raise_for_status()
    trials = resp.json()
    # Replay findings (such as a copied protected file) live on the version.
    panel = client.get(f"{api_url}/tasks/{task_id}/panel", params={"version": version})
    panel.raise_for_status()
    found = {
        finding.get("replay_trial_id")
        for finding in (panel.json().get("version") or {}).get("retained_findings")
        or []
        if finding.get("source") == "adversarial"
    }
    by_id = {t["id"]: t for t in trials}
    rows = []
    for attack_id in ids:
        attack = by_id.get(attack_id)
        if attack is None:
            # Not listed yet: keep watching rather than call it settled.
            rows.append(
                {
                    "trial_id": attack_id,
                    "mode": "-",
                    "status": "missing",
                    "reward": None,
                    "replay_trial_id": None,
                    "replay_reward": None,
                    "result": "missing",
                    "live": True,
                }
            )
            continue
        replay = next(
            (
                t
                for t in trials
                if t.get("agent") == REPLAY_AGENT
                and _pins(t).get("replay_of") == attack_id
                and _pins(t).get("attack_attempt") == attack.get("attempts")
            ),
            None,
        )
        rows.append(
            {
                "trial_id": attack_id,
                "mode": "white" if _pins(attack).get("white_box") else "black",
                "status": attack["status"],
                "reward": attack.get("reward"),
                "replay_trial_id": replay and replay["id"],
                "replay_reward": replay and replay.get("reward"),
                "result": attack_result(
                    attack, replay, bool(replay and replay["id"] in found)
                ),
                # Live until the replay is queued and its findings recorded,
                # so a leak found by the replay is not reported as no defect.
                "live": hook_pending(
                    attack, "replay_checked_attempt", attack.get("attempts")
                )
                or bool(replay and hook_pending(replay, "findings_recorded", True)),
            }
        )
    return rows


def _print_rows(rows: list[dict]) -> None:
    table = Table()
    for col in ("Trial", "Mode", "Status", "Reward", "Replay", "Result"):
        table.add_column(col)
    for row in rows:
        replay = (
            f"{row['replay_trial_id']} ({row['replay_reward']})"
            if row["replay_trial_id"]
            else "-"
        )
        style = "red" if row["result"] == "MUST FIX" else None
        table.add_row(
            row["trial_id"],
            row["mode"],
            row["status"],
            str(row["reward"]),
            replay,
            row["result"],
            style=style,
        )
    console.print(table)


def adversarial(
    task_id: Annotated[
        str, typer.Argument(help="Task to attack (its current version)")
    ],
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Attacker agent (default: server default)"),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Attacker model (default: server default)"),
    ] = None,
    env: Annotated[
        Optional[str],
        typer.Option("--env", "-e", help="modal or daytona (default: inherit)"),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch", "-w", help="Wait for the attacks and replays, then print results"
        ),
    ] = False,
    interval: Annotated[
        int, typer.Option("--interval", help="Seconds between polls with --watch")
    ] = 30,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
):
    """Run adversarial QA (black-box and white-box attacks) on a task's current version.

    One launch queues several attacks, alternating black box and white box.
    Each attack's exploit.sh is replayed alone; a passing replay is a MUST FIX.

    Examples:
        oddish adversarial <task_id>
        oddish adversarial <task_id> -m openrouter/z-ai/glm-5.3 --watch
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{api_url}/tasks/{task_id}")
        if resp.status_code != 200:
            console.print(f"[red]Task {task_id} not found:[/red] {resp.text}")
            raise typer.Exit(1)
        task = resp.json()
        body: dict = {
            "expected_version_id": task["current_version_id"],
            "request_id": str(uuid.uuid4()),
        }
        if env:
            body["environment"] = env
        if agent or model:
            body["execution"] = {"agent": agent or "auto", "model": model}
        resp = client.post(f"{api_url}/tasks/{task_id}/qa/adversarial", json=body)
        if resp.status_code != 200:
            console.print(f"[red]Launch failed ({resp.status_code}):[/red] {resp.text}")
            raise typer.Exit(1)
        launch = resp.json()
        ids = launch.get("trial_ids") or [launch["trial_id"]]
        version = task["current_version"]
        if not json_output:
            console.print(
                f"[green]{launch['status']}[/green] {len(ids)} attacks on "
                f"{task_id} v{version}: {', '.join(ids)}"
            )
        if not watch:
            if json_output:
                print_json(launch)
            return

        # Settle hooks mark each attack once its replay is queued (or it had
        # no exploit.sh) and each replay once its findings are recorded.
        rows = None
        while rows is None or any(row["live"] for row in rows):
            time.sleep(interval)
            try:
                rows = _rows(client, api_url, task_id, version, ids)
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                if 400 <= status < 500 and status not in (408, 429):
                    console.print(f"[red]Poll failed ({status}):[/red] {exc}")
                    raise typer.Exit(1)
                console.print(f"[yellow]poll failed, retrying:[/yellow] {exc}")
                continue
            if not json_output:
                done = sum(not row["live"] for row in rows)
                console.print(f"{done}/{len(ids)} settled")

    if json_output:
        print_json({**launch, "attacks": rows})
    else:
        _print_rows(rows)
