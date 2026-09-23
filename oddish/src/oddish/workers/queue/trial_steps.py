"""Lifecycle steps shared by the worker path and the monitor launcher.

Public, behaviour-preserving pieces of the trial lifecycle that both
``trial_handler.run_trial_job`` and ``workers.launcher`` call. The attempt
start is two steps under one lock: ``start_trial_attempt`` writes the row and
``load_trial_context`` builds the attempt context from the loaded rows. The
launcher's settlement calls only the builder, in its own read-only session,
for the attempt it observed; calling the starter there would restart it.
"""

from __future__ import annotations

import functools
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE, settings
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialStatus,
    is_worker_owned_trial_status,
    utcnow,
)
from oddish.workers.analysis_trials import is_analysis_kind
from oddish.workers.queue.db_helpers import _trial_session
from oddish.workers.queue.shared import console

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def installed_harbor_descriptor() -> tuple[str, str]:
    """The source and commit of the harbor THIS process imports.

    Read from the installed distribution's PEP 610 ``direct_url.json``, which
    records the resolved VCS URL and commit for a git install -- the one
    answer that is correct in every runtime: variant image, default image,
    self-host dispatcher, or local mode. Falls back to the locked default pin
    when the metadata is unavailable (a non-git install), which is what such
    an environment executes.
    """
    try:
        import importlib.metadata
        import json as _json

        raw = importlib.metadata.distribution("harbor").read_text("direct_url.json")
        info = _json.loads(raw or "")
        url = str(info.get("url") or "").removeprefix("git+").removesuffix(".git")
        sha = str((info.get("vcs_info") or {}).get("commit_id") or "")
        if url and sha:
            return url, sha
    except Exception:  # noqa: BLE001 -- any metadata shape falls back
        pass
    return HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA


def refresh_stable_variant_pin(
    trial, *, executing: tuple[str, str] | None = None
) -> dict | None:
    """Refresh a stable-variant trial's recorded harbor pin at claim time.

    The deployment is the unit of harbor identity for stable variants: the
    ``gke`` variant image bakes the blessed gke pin, and every other worker
    executes the locked default pin -- so a trial queued across a pin bump
    executes whatever the deployment now ships, and the pin stamped at
    submission can go stale while it waits. Rewrite the record the moment
    the worker claims the job so the row matches execution; the trial's own
    lock.json already told the truth, this makes the database agree with it.

    The indexed ``trials.harbor_sha`` projection is reconciled even when the
    pin itself already matches: immutable retry, combine, and import all
    persist ``harbor_config`` without the projection, and sha filters query
    only the projection.

    Left untouched: ``ephemeral`` exact-pin trials (they run the sha they
    recorded, out of process) beyond the projection sync, and configs with
    no harbor identity at all (audit/analysis payloads execute no pin).
    """
    harbor_config = getattr(trial, "harbor_config", None)
    if not isinstance(harbor_config, dict):
        return harbor_config
    variant_id = harbor_config.get("variant_id")
    if variant_id is None and "resolved_sha" not in harbor_config:
        return harbor_config
    if variant_id != "ephemeral":
        # ``executing`` is what the claiming runtime actually runs. When the
        # caller does not say, ask the runtime itself: the imported harbor's
        # installation metadata carries the resolved source and commit, which
        # is exact in every mode -- a Modal variant image bakes its blessed
        # pin, the default image bakes the locked default, and a self-host
        # or local worker executes whatever is installed, regardless of the
        # trial's variant label.
        if executing is None:
            executing = installed_harbor_descriptor()
        source, sha = executing
        if (
            harbor_config.get("resolved_sha") != sha
            or harbor_config.get("source") != source
        ):
            logger.warning(
                "trial %s: recorded harbor pin %s superseded by the deployed %s "
                "at claim",
                getattr(trial, "id", "?"),
                harbor_config.get("resolved_sha"),
                sha,
            )
            harbor_config = {
                **harbor_config,
                "source": source,
                "resolved_sha": sha,
            }
            trial.harbor_config = harbor_config
    resolved = harbor_config.get("resolved_sha")
    if hasattr(trial, "harbor_sha") and trial.harbor_sha != resolved:
        trial.harbor_sha = resolved
    return harbor_config


def extract_trial_index(trial_id: str, task_id: str) -> int:
    """Extract the 0-based trial index from a trial ID like '{task_id}-{index}'."""
    suffix = trial_id[len(task_id) :]  # e.g., "-0", "-1", "-2"
    if suffix.startswith("-") and suffix[1:].isdigit():
        return int(suffix[1:])
    return 0


def artifact_subprefix(trial_kind: str, trial_attempt: int) -> str:
    """Return the immutable storage path owned by one trial attempt.

    Analysis trials also carry a self-labeling segment because they share the
    subject task's trial-id sequence and storage neighborhood. The attempt
    segment prevents a retry from replacing the manifest while leaving older
    randomly-named Harbor trial directories beside it.
    """
    if trial_attempt < 1:
        raise ValueError(f"trial attempt must be positive, got {trial_attempt}")
    attempt = f"attempt-{trial_attempt}"
    if is_analysis_kind(trial_kind):
        return f"analysis-{trial_kind}/{attempt}"
    return attempt


@dataclass(slots=True)
class PreparedTrialRun:
    task_path: str | None
    task_s3_key: str | None
    task_id: str
    trial_agent: str
    trial_model: str
    trial_environment: str | None
    trial_harbor_config: dict | None
    fallback_from_environment: str | None = None
    trial_kind: str = "agent"
    task_version: int | None = None
    task_version_id: str | None = None
    # Fields for sauron S3 mirror
    task_name: str = ""
    experiment_id: str = ""
    experiment_name: str | None = None
    attempt_number: int = 1
    task_tags: dict | None = None
    # Owning org, used to stage that org's shared skills into the probe sandbox.
    org_id: str | None = None
    # Trial owner for BYOK resolution: the task submitter, falling back to the
    # experiment owner. None means BYOK never applies.
    created_by_user_id: str | None = None
    billed_user_id: str | None = None
    trial_attempt: int = 1


# Maximum length we persist for last_heartbeat_error. We truncate aggressively
# because it's for operator diagnosis, not full stack traces.
_HEARTBEAT_ERROR_MAX_LEN = 500


async def touch_trial_execution(
    *,
    trial_id: str,
    worker_id: str | None,
    queue_slot: int | None,
    claimed: bool = False,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
    pending_last_error_at: datetime | None = None,
) -> bool:
    """Update the trial heartbeat row."""
    async with _trial_session(trial_id, allow_missing=True, with_for_update=True) as (
        session,
        trial,
    ):
        if not trial or not is_worker_owned_trial_status(trial.status):
            return False
        if trial.superseded_by_trial_id is not None:
            return False
        if worker_id and trial.current_worker_id not in (None, worker_id):
            return False

        now = utcnow()
        trial.current_worker_id = worker_id
        trial.current_queue_slot = queue_slot
        if claimed:
            trial.claimed_at = now
        trial.heartbeat_at = now

        if pending_failure_count > 0:
            trial.heartbeat_failure_count = (
                trial.heartbeat_failure_count or 0
            ) + pending_failure_count
            if pending_last_error is not None:
                trial.last_heartbeat_error = pending_last_error[
                    :_HEARTBEAT_ERROR_MAX_LEN
                ]
            if pending_last_error_at is not None:
                trial.last_heartbeat_error_at = pending_last_error_at
        return True


async def start_trial_attempt(
    session, trial, *, worker_id: str | None, queue_slot: int | None
) -> None:
    """Start the next attempt on ``trial``, a row the caller holds FOR UPDATE.

    Every write of an attempt start: status RUNNING, the previous attempt's
    result fields reset, ``attempts`` incremented, the model and queue key
    normalised, the harbor pin refreshed, the worker recorded as owner. The
    caller builds the context with ``load_trial_context`` in the same
    transaction. Settlement must never call this.
    """
    # Apply at pickup so recovered worker failures follow the same fallback
    # as settled failures. Persist the provider for every remaining retry.
    if (
        trial.status == TrialStatus.RETRYING
        and 2 <= trial.attempts < trial.max_attempts
        and (trial.environment or settings.harbor_environment) == "archil"
    ):
        trial.environment = "modal"

    trial.status = TrialStatus.RUNNING
    trial.started_at = utcnow()
    trial.finished_at = None
    trial.next_retry_at = None
    trial.harbor_stage = "starting"
    trial.reward = None
    trial.error_message = None
    trial.harbor_result_path = None
    trial.trial_s3_key = None
    trial.result = None
    trial.input_tokens = None
    trial.cache_tokens = None
    trial.cache_write_tokens = None
    trial.output_tokens = None
    trial.total_steps = None
    trial.trajectory_duration_seconds = None
    trial.total_tool_calls = None
    trial.tool_counts = None
    trial.cost_usd = None
    trial.phase_timing = None
    trial.has_trajectory = False
    # A stale analysis from an earlier attempt is dropped in
    # ``_run_post_trial_hooks``, not here: clearing it at attempt start would
    # leave the trial unclassified for the whole attempt, racing an in-flight
    # QA job that snapshots its work list up front.
    trial.attempts += 1

    if not trial.idempotency_key:
        trial.idempotency_key = str(uuid.uuid4())

    task = await session.get(TaskModel, trial.task_id)
    if task and task.status == TaskStatus.PENDING:
        task.status = TaskStatus.RUNNING
        task.started_at = utcnow()

    trial_agent = trial.agent
    trial_model = settings.normalize_trial_model(trial_agent, trial.model)
    if trial.model != trial_model:
        trial.model = trial_model
    canonical_queue_key = settings.get_queue_key_for_trial(trial_agent, trial_model)
    if trial.queue_key != canonical_queue_key:
        trial.queue_key = canonical_queue_key
    refresh_stable_variant_pin(trial)
    trial.current_worker_id = worker_id
    trial.current_queue_slot = queue_slot
    trial.claimed_at = utcnow()
    trial.heartbeat_at = trial.claimed_at
    # ``modal_function_call_id`` now lives exclusively on
    # ``worker_jobs``. The claim SQL stamped it; the cancel path
    # harvests it from ``worker_jobs.RETURNING``.


async def load_trial_context(
    session, trial, *, fallback_from_environment: str | None = None
) -> PreparedTrialRun:
    """The attempt context, from the rows as loaded. Reads only.

    The task path and archive come from the trial's pinned task version when
    it has one, never from the current head. On the launch path this runs
    after ``start_trial_attempt`` in the same locked transaction, so it sees
    the normalised model and refreshed pin; settlement runs it alone on the
    attempt it observed.
    """
    task = await session.get(TaskModel, trial.task_id)
    task_id = task.id if task else trial.task_id
    task_name = task.name if task else trial.task_id
    task_tags = dict(task.tags) if task and task.tags else None

    experiment_id = trial.experiment_id or ""
    experiment_name: str | None = None
    experiment_owner_user_id: str | None = None
    if experiment_id:
        experiment = await session.get(ExperimentModel, experiment_id)
        if experiment:
            experiment_name = experiment.name
            experiment_owner_user_id = experiment.owner_user_id

    task_path: str | None = None
    task_s3_key: str | None = None
    task_version: int | None = None
    if trial.task_version_id:
        tv = await session.get(TaskVersionModel, trial.task_version_id)
        if tv:
            task_path = tv.task_path
            task_s3_key = tv.task_s3_key
            task_version = tv.version
    if task_path is None and task:
        task_path = task.task_path
    if task_s3_key is None and task:
        task_s3_key = task.task_s3_key
    return PreparedTrialRun(
        task_path=task_path,
        task_s3_key=task_s3_key,
        task_id=task_id,
        trial_agent=trial.agent,
        trial_model=trial.model,
        trial_environment=trial.environment,
        trial_harbor_config=trial.harbor_config,
        fallback_from_environment=fallback_from_environment,
        trial_kind=trial.kind or "agent",
        task_version=task_version,
        task_version_id=trial.task_version_id,
        task_name=task_name,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        # Extract trial index from trial_id ("{task_id}-{index}") for the
        # sauron attempt number. This is the trial's position within its
        # task (0, 1, 2...), NOT the retry count (trial.attempts).
        # Multiple trials of the same task must map to different attempt_N
        # folders to avoid overwriting each other.
        attempt_number=extract_trial_index(trial.id, task_id) + 1,  # 1-indexed
        task_tags=task_tags,
        org_id=trial.org_id,
        billed_user_id=trial.billed_user_id,
        trial_attempt=trial.attempts,
        created_by_user_id=(
            (task.created_by_user_id if task else None) or experiment_owner_user_id
        ),
    )


async def prepare_trial_run(
    *,
    trial_id: str,
    worker_id: str | None,
    queue_slot: int | None,
    modal_function_call_id: str | None,
    fallback_from_environment: str | None = None,
) -> PreparedTrialRun | None:
    """Claim the trial for a new attempt: lock the row, start the attempt and
    build its context in one transaction. ``None`` when there is nothing to
    run (missing or superseded)."""
    async with _trial_session(trial_id, with_for_update=True) as (session, trial):
        if not trial:
            console.print(f"[yellow]Trial {trial_id} not found, skipping[/yellow]")
            return None
        if trial.superseded_by_trial_id is not None:
            console.print(f"[dim]Trial {trial_id} was superseded, skipping[/dim]")
            return None
        await start_trial_attempt(
            session, trial, worker_id=worker_id, queue_slot=queue_slot
        )
        prepared = await load_trial_context(
            session, trial, fallback_from_environment=fallback_from_environment
        )
        await refresh_task_browse_summaries(session, [trial.task_version_id])
        return prepared
