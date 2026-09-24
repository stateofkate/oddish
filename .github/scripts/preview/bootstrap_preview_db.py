"""Upgrade a Neon preview without replacing its schema or saved data.

Neon copies staging's schema and rows when it creates a preview branch. Every
run applies only pending migrations. After the first successful upgrade, one
transaction stops inherited work and records the child branch ID. Later deploys
keep the preview's jobs, credentials, and saved data.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
ODDISH_DIR = REPO_ROOT / "oddish"
BACKEND_DIR = REPO_ROOT / "backend"
# Use the same order as production: oddish first, then backend.
STACKS = (ODDISH_DIR, BACKEND_DIR)
ALEMBIC_VERSION_TABLES = ("alembic_version_oddish", "alembic_version_backend")
REQUIRED_STAGING_TABLES = {"tasks", "trials", "api_keys", *ALEMBIC_VERSION_TABLES}
INITIALIZATION_TABLE = "_oddish_preview_initialization"
INHERITED_WORK_ERROR = "Stopped in this preview when copied from staging."


def _upgrade_head(project: Path) -> None:
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


def _branch_db_url() -> str:
    url = os.environ["ODDISH_DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _preview_branch_ids() -> tuple[str, str]:
    """Require the child identity supplied by the Neon branch helper."""
    branch_id = os.environ.get("NEON_BRANCH_ID", "").strip()
    parent_id = os.environ.get("NEON_PARENT_BRANCH_ID", "").strip()
    if not branch_id or not parent_id or branch_id == parent_id:
        raise RuntimeError(
            "bootstrap_preview_db: NEON_BRANCH_ID and NEON_PARENT_BRANCH_ID "
            "must identify different branches. Use the Neon preview branch "
            "helper; this script must not run on staging."
        )
    return branch_id, parent_id


def _assert_preview_branch(url: str) -> None:
    """Shared guard for preview-only post-migration commands."""
    _preview_branch_ids()
    if url != _branch_db_url():
        raise RuntimeError("Preview command must use the selected Neon branch URL")


def _engine(url: str):
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


def _load_base():
    """Register both model stacks for the schema check."""
    for path in (BACKEND_DIR, ODDISH_DIR / "src"):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    import models  # noqa: F401
    from oddish.db.models import Base

    return Base


async def _assert_staging_snapshot(url: str) -> None:
    """Refuse an empty clone before Alembic can turn it into an empty app DB."""
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                ).scalars()
            )
            missing = REQUIRED_STAGING_TABLES - tables
            if not missing:
                for table in ALEMBIC_VERSION_TABLES:
                    has_revision = await conn.scalar(
                        text(
                            f"SELECT EXISTS (SELECT 1 FROM public.{table} "
                            "WHERE version_num IS NOT NULL AND version_num <> '')"
                        )
                    )
                    if not has_revision:
                        missing.add(f"{table} revision")
            if missing:
                raise RuntimeError(
                    "bootstrap_preview_db: this preview has no complete staging copy "
                    f"(missing: {', '.join(sorted(missing))}). Finish the local staging data move, "
                    "then run Preview Reset for this PR to copy staging again. "
                    "No migrations or data changes were run."
                )
    finally:
        await engine.dispose()


async def _initialize_clone(url: str, branch_id: str, parent_id: str) -> bool:
    """Stop copied work once, in the same transaction as the branch marker.

    The marker uses the actual Neon child ID. A marker inherited from a parent
    cannot skip this step. The lock serializes concurrent first runs, and a
    failure rolls back both the cleanup and marker so a retry can finish it.
    No remote worker or sandbox is stopped: those belong to staging.
    """
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
                {"lock_name": "oddish-preview-initialize"},
            )
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS public.{INITIALIZATION_TABLE} ("
                    "branch_id text PRIMARY KEY, parent_branch_id text NOT NULL, "
                    "initialized_at timestamptz NOT NULL DEFAULT now())"
                )
            )
            initialized = await conn.scalar(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM public.{INITIALIZATION_TABLE} "
                    "WHERE branch_id = :branch_id)"
                ),
                {"branch_id": branch_id},
            )
            if initialized:
                return False

            changed_versions = (
                (
                    await conn.execute(
                        text(_STOP_INHERITED_TRIALS_SQL),
                        {"reason": INHERITED_WORK_ERROR},
                    )
                )
                .scalars()
                .all()
            )
            for statement in _CLONE_INITIALIZATION_SQL:
                await conn.execute(text(statement), {"reason": INHERITED_WORK_ERROR})
            await _refresh_initialized_versions(conn, changed_versions)

            # Last write, but in the same transaction as all inherited changes.
            await conn.execute(
                text(
                    f"INSERT INTO public.{INITIALIZATION_TABLE} "
                    "(branch_id, parent_branch_id) VALUES (:branch_id, :parent_id)"
                ),
                {"branch_id": branch_id, "parent_id": parent_id},
            )
    finally:
        await engine.dispose()
    return True


async def _refresh_initialized_versions(conn, task_version_ids) -> None:
    """Refresh only stopped trials' versions before the setup marker commits."""
    version_ids = sorted({str(value) for value in task_version_ids if value})
    if not version_ids:
        return
    from oddish.core.task_browse_summary import refresh_task_browse_summaries

    # The connection owns this transaction. The session must not commit or
    # close it: a refresh error must also roll back the trial stops and marker.
    async with AsyncSession(
        bind=conn, join_transaction_mode="rollback_only"
    ) as session:
        for start in range(0, len(version_ids), 200):
            await refresh_task_browse_summaries(
                session, version_ids[start : start + 200]
            )
    print(f"bootstrap_preview_db: refreshed {len(version_ids)} stopped trial versions")


_STOP_INHERITED_TRIALS_SQL = """
    UPDATE public.trials
    SET status = 'FAILED', finished_at = now(), updated_at = now(),
        error_message = :reason, harbor_stage = 'cancelled', next_retry_at = NULL,
        current_worker_id = NULL, current_queue_slot = NULL,
        claimed_at = NULL, heartbeat_at = NULL
    WHERE status IN ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')
    RETURNING task_version_id
"""


# Use explicit status sets so completed history keeps its results and dates.
# These statements run only after both migration stacks and the schema check.
_CLONE_INITIALIZATION_SQL = (
    """
    UPDATE public.worker_jobs
    SET status = 'CANCELLED', finished_at = now(), updated_at = now(),
        error_message = :reason, next_retry_at = NULL,
        current_worker_id = NULL, current_queue_slot = NULL,
        provider = NULL, modal_function_call_id = NULL, external_id = NULL,
        reroute_pending_teardown = false, claimed_at = NULL, heartbeat_at = NULL,
        job_token_hash = NULL, job_token_expires_at = NULL,
        job_token_revoked_at = now(), monitor_token_hash = NULL,
        monitor_token_expires_at = NULL, monitor_token_revoked_at = now()
    WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED')
    """,
    """
    UPDATE public.sandbox_runs
    SET state = 'TERMINATED', terminated_at = now(), last_error = :reason,
        updated_at = now()
    WHERE state IN ('PROVISIONING', 'RUNNING', 'TERMINATING')
    """,
    """
    UPDATE public.sandbox_runs SET external_id = NULL
    WHERE external_id IS NOT NULL
    """,
    """
    UPDATE public.trials
    SET analysis_status = 'FAILED', analysis_error = :reason,
        analysis_finished_at = now(), updated_at = now()
    WHERE analysis_status IN ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')
    """,
    """
    UPDATE public.task_versions
    SET pre_trial_status = CASE
            WHEN pre_trial IS NULL OR pre_trial = 'null'::jsonb THEN 'FAILED'::jobstatus
            ELSE 'SUCCESS'::jobstatus
        END, pre_trial_error = :reason,
        pre_trial_finished_at = now(), updated_at = now()
    WHERE pre_trial_status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
    """,
    """
    UPDATE public.tasks
    SET status = 'FAILED', finished_at = now(), updated_at = now()
    WHERE status IN ('PENDING', 'RUNNING', 'ANALYZING', 'VERDICT_PENDING')
    """,
    """
    UPDATE public.tasks
    SET verdict_status = CASE
            WHEN verdict IS NULL OR verdict = 'null'::jsonb THEN 'FAILED'::jobstatus
            ELSE 'SUCCESS'::jobstatus
        END, verdict_error = :reason,
        verdict_finished_at = now(), updated_at = now()
    WHERE verdict_status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
    """,
    """
    UPDATE public.api_keys
    SET is_active = false, updated_at = now()
    WHERE is_active
    """,
    "DELETE FROM public.queue_slots",
    "DELETE FROM public.sandbox_capacity_leases",
    "DELETE FROM public.model_request_leases",
    "DELETE FROM public.model_request_pools",
    "DELETE FROM public.queue_dispatch_state",
    "DELETE FROM public.queue_runtime_status",
    "DELETE FROM public.trial_events",
    "DELETE FROM public.submission_idempotency",
)


async def _assert_model_schema(url: str) -> None:
    """Every model table, column, and named index must exist in the schema.

    Catches model-without-migration drift right on the PR that introduces it.
    Constraint names are deliberately not compared: prod's historical FK/unique
    names legitimately differ from model-generated ones.
    """
    metadata = _load_base().metadata
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    """
                )
            )
            actual_columns: dict[str, set[str]] = {}
            for table_name, column_name in rows:
                actual_columns.setdefault(table_name, set()).add(column_name)
            index_rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
            actual_indexes = {row[0] for row in index_rows}
    finally:
        await engine.dispose()

    missing: list[str] = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        columns = actual_columns.get(table.name)
        if columns is None:
            missing.append(table.name)
            continue
        missing.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in columns
        )
        missing.extend(
            f"index {index.name}"
            for index in table.indexes
            if index.name and index.name not in actual_indexes
        )
    if missing:
        preview = ", ".join(missing[:20])
        extra = "" if len(missing) <= 20 else f", and {len(missing) - 20} more"
        raise RuntimeError(f"preview schema is missing model objects: {preview}{extra}")


def _upgrade_and_verify(url: str) -> None:
    for project in STACKS:
        _upgrade_head(project)
    asyncio.run(_assert_model_schema(url))


def main() -> None:
    branch_id, parent_id = _preview_branch_ids()
    url = _branch_db_url()
    asyncio.run(_assert_staging_snapshot(url))
    try:
        _upgrade_and_verify(url)
    except Exception as exc:
        raise RuntimeError(
            "bootstrap_preview_db: preview migration or schema check failed; "
            "no reset was run. Fix the migration or restore any missing "
            "revision file, then run the deploy again. Applied migrations "
            "must stay in the branch history; add a new migration to change them."
        ) from exc
    initialized = asyncio.run(_initialize_clone(url, branch_id, parent_id))
    print(
        "bootstrap_preview_db: migrations passed; "
        + ("staging copy initialized" if initialized else "preview data kept"),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
