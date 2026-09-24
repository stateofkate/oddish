"""hard-delete probe trials; drop probe columns, skills, and documents

Revision ID: probe_removal_001
Revises: version_verdict_001
Create Date: 2026-09-24 00:00:00.000000

Operator probes, the skills library, and the document store are removed.
Probe trials (``trials.is_probe``) are hard-deleted together with every row
that names them: the trial-keyed ledgers, their ``worker_jobs``, and the
job-keyed rows under those jobs. ``sandbox_runs`` goes before
``worker_jobs`` because its foreign key is ``ON DELETE RESTRICT``;
``experiment_trials`` and ``trial_events`` cascade with ``trials``, and
``trials.superseded_by_trial_id`` is ``SET NULL``. Then the probe indexes,
``trials.is_probe``, ``tasks.run_probe``, and the ``skill_files``,
``skills`` and ``documents`` tables are dropped, as is the write-only
``task_version_model_metrics`` rollup. The column drop also drops
``idx_trials_task_browse_preview`` (task_browse_summary_001), whose predicate
names ``is_probe``, so it is rebuilt on the same scope without that clause.
S3 objects (trial logs and uploaded documents) are not touched.

Each statement runs in its own autocommit transaction under a short
lock_timeout with retries (see ``migration_locks``) and is idempotent, so a
failed run is safe to re-trigger. The deletes are skipped once
``trials.is_probe`` is gone.

Downgrade is not supported: the deleted rows cannot be restored.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db.migration_locks import run_with_lock_retry

revision: str = "probe_removal_001"
down_revision: Union[str, Sequence[str], None] = "version_verdict_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROBE_TRIAL_IDS = "SELECT id FROM trials WHERE is_probe"
_PROBE_JOB_IDS = (
    "SELECT id FROM worker_jobs WHERE subject_table = 'trials' "
    f"AND subject_id IN ({_PROBE_TRIAL_IDS})"
)
# (table, column, ids) in delete order: dependents first, trials last.
_PROBE_DELETES = (
    ("sandbox_runs", "trial_id", _PROBE_TRIAL_IDS),
    ("sandbox_runs", "worker_job_id", _PROBE_JOB_IDS),
    ("modal_costs", "trial_id", _PROBE_TRIAL_IDS),
    ("modal_costs", "worker_job_id", _PROBE_JOB_IDS),
    ("worker_resource_attempts", "worker_job_id", _PROBE_JOB_IDS),
    ("model_request_leases", "worker_job_id", _PROBE_JOB_IDS),
    ("worker_jobs", "id", _PROBE_JOB_IDS),
    ("analysis_costs", "trial_id", _PROBE_TRIAL_IDS),
    ("verifier_costs", "trial_id", _PROBE_TRIAL_IDS),
    ("feedback", "trial_id", _PROBE_TRIAL_IDS),
    ("api_keys", "bound_analysis_trial_id", _PROBE_TRIAL_IDS),
    ("trials", "id", _PROBE_TRIAL_IDS),
)


def _has_column(table: str, column: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table)
    return column in {item["name"] for item in columns}


def _run(table: str, sql: str) -> None:
    run_with_lock_retry(lambda: op.execute(sql), table_name=table)


def _rebuild_browse_preview_index() -> None:
    # An interrupted CONCURRENTLY build leaves an INVALID index that
    # ``IF NOT EXISTS`` would keep; drop it so the retry rebuilds it.
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'idx_trials_task_browse_preview' "
                "AND NOT i.indisvalid"
            )
        )
        .first()
    )
    if invalid is not None:
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_trials_task_browse_preview")
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trials_task_browse_preview "
        "ON trials (task_id, task_version_id, created_at DESC, id DESC) "
        "WHERE deleted_at IS NULL AND superseded_by_trial_id IS NULL "
        "AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')"
    )


def upgrade() -> None:
    if _has_column("trials", "is_probe"):
        for table, column, ids in _PROBE_DELETES:
            _run(table, f"DELETE FROM {table} WHERE {column} IN ({ids})")
    for index in ("idx_trials_probe_org_task_created", "ix_trials_is_probe"):
        _run("trials", f"DROP INDEX CONCURRENTLY IF EXISTS {index}")
    _run("trials", "ALTER TABLE trials DROP COLUMN IF EXISTS is_probe")
    run_with_lock_retry(_rebuild_browse_preview_index, table_name="trials")
    _run("tasks", "ALTER TABLE tasks DROP COLUMN IF EXISTS run_probe")
    for table in ("skill_files", "skills", "documents", "task_version_model_metrics"):
        _run(table, f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    raise RuntimeError(
        "probe_removal_001 deletes probe trials, skills, and documents; use a forward migration."
    )
