"""Copy uncapped legacy usage in bounded batches, or recover one trial's artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json

from harbor.models.trial.result import TrialResult
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.harbor_artifacts import cache_write_tokens_from_trajectory
from oddish.core.trial_artifacts import TrialArtifactMode, resolve_trial_artifact_layout
from oddish.core.trial_io import read_trial_trajectory
from oddish.db import TrialModel
from oddish.db.connection import get_session
from oddish.db.models import INT32_MAX
from oddish.db.storage import get_storage_client

TOKEN_COLUMNS = {
    "trials": ("input_tokens", "output_tokens", "cache_tokens", "cache_write_tokens"),
    "analysis_costs": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
    "verifier_costs": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
}


async def backfill_batch(
    session: AsyncSession, *, table: str, after: str = "", limit: int = 500
) -> dict:
    columns = TOKEN_COLUMNS[table]
    if not 1 <= limit <= 5000:
        raise ValueError("batch size must be between 1 and 5000")
    terminal = "AND finished_at IS NOT NULL" if table == "trials" else ""
    ids = list(
        await session.scalars(
            text(f"""
        SELECT id FROM {table} WHERE id > :after {terminal}
        ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED
    """),
            {"after": after, "limit": limit},
        )
    )
    updated = 0
    for column in columns:
        result = await session.execute(
            text(f"""
            UPDATE {table} SET {column}_exact = {column}::bigint
            WHERE id = ANY(:ids) AND {column}_exact IS NULL
                AND {column} >= 0 AND {column} < :cap
        """),
            {"ids": ids, "cap": INT32_MAX},
        )
        updated += result.rowcount
    return {
        "scanned": len(ids),
        "fields_updated": updated,
        "after": ids[-1] if ids else after,
    }


async def recover_trial(session: AsyncSession, *, trial_id: str) -> dict:
    trial = await session.get(TrialModel, trial_id)
    if trial is None or trial.finished_at is None:
        raise ValueError("recovery requires an existing finished trial")
    if not trial.trial_s3_key or not trial.trial_s3_key.rstrip("/").endswith(
        f"/attempt-{trial.attempts}"
    ):
        raise ValueError(
            "recovery requires the stored current-attempt artifact pointer"
        )
    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is not TrialArtifactMode.EXACT or layout.artifact_prefix is None:
        raise ValueError("recovery requires an authoritative selected Harbor trial")
    result = TrialResult.model_validate_json(
        await storage.download_text(f"{layout.artifact_prefix}result.json")
    )
    if result.trial_name != layout.artifact_prefix.rstrip("/").rsplit("/", 1)[-1]:
        raise ValueError("artifact result does not match the selected Harbor trial")
    input_tokens, cache_tokens, output_tokens, _ = result.compute_token_cost_totals()
    trajectory = await read_trial_trajectory(trial)
    recovered = {
        "input_tokens": input_tokens,
        "cache_tokens": cache_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": cache_write_tokens_from_trajectory(trajectory),
    }
    values = {}
    for column, value in recovered.items():
        if value is None or getattr(trial, f"{column}_exact") is not None:
            continue
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError(f"artifact {column} must be a nonnegative 64-bit integer")
        legacy = getattr(trial, f"{column}_legacy")
        if legacy is not None and value < legacy:
            raise ValueError(f"artifact {column} is below the stored lower bound")
        values[f"{column}_exact"] = value
    if not values:
        return {"trial_id": trial_id, "recovered": []}
    # No row lock during storage I/O. Any intervening checkpoint, retry, repair
    # or settlement must make this compare-and-set fail instead of losing usage.
    conditions = [
        TrialModel.id == trial_id,
        TrialModel.updated_at == trial.updated_at,
        TrialModel.trial_s3_key == trial.trial_s3_key,
        TrialModel.attempts == trial.attempts,
        TrialModel.finished_at == trial.finished_at,
    ]
    for column in TOKEN_COLUMNS["trials"]:
        conditions.extend(
            [
                getattr(TrialModel, f"{column}_exact").is_not_distinct_from(
                    getattr(trial, f"{column}_exact")
                ),
                getattr(TrialModel, f"{column}_legacy").is_not_distinct_from(
                    getattr(trial, f"{column}_legacy")
                ),
            ]
        )
    written = await session.execute(
        update(TrialModel)
        .where(*conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if written.rowcount != 1:
        raise ValueError(
            "trial changed during recovery; read the new attempt and retry"
        )
    return {"trial_id": trial_id, "recovered": values}


async def main(args: argparse.Namespace) -> None:
    async with get_session() as session:
        if args.recover_trial:
            result = await recover_trial(session, trial_id=args.recover_trial)
        else:
            result = await backfill_batch(
                session, table=args.table, after=args.after, limit=args.limit
            )
        if not args.apply:
            await session.rollback()
        print(json.dumps({"applied": args.apply, **result}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--table", choices=TOKEN_COLUMNS)
    target.add_argument("--recover-trial")
    parser.add_argument("--after", default="")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args()))
