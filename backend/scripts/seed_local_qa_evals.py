"""Copy named trials from a deployed API into a local database (dev only).

From ``backend/``, with ``ODDISH_DATABASE_URL`` pointing at a local Postgres
migrated by both stacks and a key for the remote API in ``$ODDISH_API_KEY``::

    uv run python scripts/seed_local_qa_evals.py --trials goldens.csv \\
        --comparison-run comparison-run.json

``--trials`` takes a list of ids, one per line, or a CSV/TSV with a
``source_trial_id`` or ``trial_id`` column. ``--comparison-run`` takes a
replay comparison's JSON and adds its source and replay trials and the names
of their experiments. The trials' tasks, task versions and experiments come
along. The remote API is only read. Everything lands in the local org
``qa-dev-org``; existing rows are kept, so reruns are safe. A new local API
key for that org is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import OrganizationModel, UserModel, UserRole
from oddish.core.api_keys import create_api_key
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    get_session,
)

LOCAL_ORG = "qa-dev-org"
LOCAL_USER = "qa-dev-user"
TRIAL_FIELDS = (
    "id name task_id task_version_id experiment_id agent provider queue_key "
    "model kind reward error_message analysis analysis_error cost_usd "
    "harbor_config has_trajectory"
).split()


def _trial_ids(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or "trial_id" not in lines[0]:
        return [line.strip() for line in lines if line.strip()]
    rows = csv.DictReader(lines, dialect="excel-tab" if "\t" in lines[0] else "excel")
    ids = (row.get("source_trial_id") or row.get("trial_id") or "" for row in rows)
    return [trial_id.strip() for trial_id in ids if trial_id.strip()]


def _comparison_run(path: Path) -> tuple[list[str], dict[str, str]]:
    """The run's source and replay trial ids, and its experiments' names."""
    ids: list[str] = []
    names: dict[str, str] = {}
    for variant in json.loads(path.read_text(encoding="utf-8-sig")).get("variants", []):
        for pair in variant.get("trials") or []:
            ids += [pair["source_trial_id"], pair["qa_eval_trial_id"]]
        if variant.get("experiment_id"):
            names[variant["experiment_id"]] = variant.get(
                "experiment_name"
            ) or variant.get("label", "")
    return ids, names


async def _fetch(client: httpx.AsyncClient, paths: list[str]) -> dict[str, Any]:
    limit = asyncio.Semaphore(8)

    async def one(path: str) -> tuple[str, Any]:
        async with limit:
            response = await client.get(path)
        if response.status_code == 404:
            print(f"  missing remotely: {path}")
            return path, None
        response.raise_for_status()
        return path, response.json()

    return dict(await asyncio.gather(*(one(path) for path in paths)))


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def main(args: argparse.Namespace) -> None:
    url = os.environ.get("ODDISH_DATABASE_URL", "").replace("+asyncpg", "")
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("refusing to seed a database that is not on localhost")
    trial_ids: list[str] = []
    experiment_names: dict[str, str] = {}
    for path in args.trials:
        trial_ids += _trial_ids(path)
    for path in args.comparison_run:
        ids, names = _comparison_run(path)
        trial_ids += ids
        experiment_names |= names
    trial_ids = list(dict.fromkeys(trial_ids))
    print(f"fetching {len(trial_ids)} trials from {args.api}")

    headers = {"Authorization": f"Bearer {os.environ[args.api_key_env]}"}
    async with httpx.AsyncClient(
        base_url=args.api, headers=headers, timeout=60
    ) as client:
        fetched = await _fetch(client, [f"/trials/{i}" for i in trial_ids])
        trials = [trial for trial in fetched.values() if trial]
        task_ids = sorted({trial["task_id"] for trial in trials})
        versions = await _fetch(client, [f"/tasks/{t}/versions" for t in task_ids])

    # An experiment starts with its first trial and, for the dashboard's
    # ordering, was last active when its last trial finished.
    started: dict[str, str] = {}
    latest: dict[str, str] = {}
    for trial in trials:
        key, created = trial.get("experiment_id"), trial.get("created_at")
        if key and created:
            started[key] = min(started.get(key, created), created)
            seen = trial.get("finished_at") or created
            latest[key] = max(latest.get(key, seen), seen)

    async with get_session() as session:

        async def insert(model: type, rows: list[dict[str, Any]]) -> None:
            if rows:
                await session.execute(pg_insert(model).on_conflict_do_nothing(), rows)

        # Hosted execution is operator-granted; the API refuses an org without it.
        org = {"id": LOCAL_ORG, "name": "QA evals dev", "slug": LOCAL_ORG}
        await insert(OrganizationModel, [org | {"execution_enabled": True}])
        user = {"id": LOCAL_USER, "org_id": LOCAL_ORG, "role": UserRole.ADMIN}
        await insert(
            UserModel, [user | {"email": "qa-dev@localhost", "name": "QA dev"}]
        )
        owned = {"org_id": LOCAL_ORG, "user": LOCAL_USER}
        await insert(
            TaskModel,
            [
                owned
                | {"id": t, "name": t, "task_path": t, "status": TaskStatus.COMPLETED}
                for t in task_ids
            ],
        )
        await insert(
            TaskVersionModel,
            [
                {"task_id": t, "content_hash": row.get("content_hash")}
                | {field: row[field] for field in ("id", "version", "task_path")}
                for t in task_ids
                for row in versions.get(f"/tasks/{t}/versions") or []
            ],
        )
        # The task page opens a ``?trial=`` drawer only once its task has a
        # current version.
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id.in_(task_ids), TaskModel.current_version_id.is_(None))
            .values(
                current_version_id=select(TaskVersionModel.id)
                .where(TaskVersionModel.task_id == TaskModel.id)
                .order_by(TaskVersionModel.version.desc())
                .limit(1)
                .scalar_subquery()
            )
        )
        await insert(
            ExperimentModel,
            [
                {
                    "id": experiment_id,
                    "name": experiment_names.get(experiment_id)
                    or f"imported {experiment_id}",
                    "org_id": LOCAL_ORG,
                    "owner_user_id": LOCAL_USER,
                    "created_at": _when(created_at),
                    "last_activity_at": _when(latest[experiment_id]),
                }
                for experiment_id, created_at in started.items()
            ],
        )
        await insert(
            TrialModel,
            [
                {field: trial.get(field) for field in TRIAL_FIELDS}
                | {
                    "org_id": LOCAL_ORG,
                    "status": TrialStatus(trial["status"]),
                    "analysis_status": trial.get("analysis_status")
                    and AnalysisStatus(trial["analysis_status"]),
                    "created_at": _when(trial.get("created_at")),
                    "finished_at": _when(trial.get("finished_at")),
                }
                for trial in trials
            ],
        )
        key, raw_key = create_api_key(
            LOCAL_ORG, "qa-evals local dev", created_by_user_id=LOCAL_USER
        )
        session.add(key)
        await session.commit()
    print(f"seeded {len(trials)} trials across {len(task_ids)} tasks into {LOCAL_ORG}")
    print(f"local API key (org {LOCAL_ORG}): {raw_key}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="https://abundant-ai--api.modal.run")
    parser.add_argument("--api-key-env", default="ODDISH_API_KEY")
    parser.add_argument("--trials", type=Path, action="append", default=[])
    parser.add_argument("--comparison-run", type=Path, action="append", default=[])
    asyncio.run(main(parser.parse_args()))
