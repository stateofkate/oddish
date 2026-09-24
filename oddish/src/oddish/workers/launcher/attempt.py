"""Build the monitor's attempt spec from the rows the worker reads at claim.

Read-only. The launcher has already claimed the trial (``prepare_trial_run``),
so the model is normalized and ``attempts`` already counts this attempt.
"""

from __future__ import annotations

import hashlib
import json
from typing import NamedTuple

from pydantic import BaseModel
from sqlalchemy import update

from oddish.db import (
    TaskModel,
    TaskVersionModel,
    TrialModel,
    WorkerJobModel,
    get_read_session,
    get_session,
)
from oddish.db.models import utcnow
from oddish.db.storage import (
    StorageClient,
    get_storage_client,
    normalize_s3_prefix,
    resolve_s3_key,
)
from oddish.mon.contract import TASK_FILES_VERSION_LENGTH, AttemptSpec, WarmRequest
from oddish.workers.launcher.token import MonitorAttempt


def _with_prebuilt_image(harbor_config: dict | None, job) -> dict | None:
    """Hand over the image the launcher resolved, unless the trial names one.

    ``docker_image`` makes Harbor pull instead of build, so the task image is
    not rebuilt inside every fresh sandbox.
    """
    image = ((job.payload or {}) if job is not None else {}).get("monitor_docker_image")
    if not image or (harbor_config or {}).get("docker_image"):
        return harbor_config
    return {**(harbor_config or {}), "docker_image": image}


def select_task_objects(prefix: str, keys: list[str]) -> tuple[str | None, list[str]]:
    """Pick what to fetch, in the order ``download_task_directory`` does.

    An archive at the prefix wins; else the newest archive at a versioned
    sub-path; else every loose object under the prefix.
    """
    archive_name = StorageClient.TASK_ARCHIVE_OBJECT_NAME
    top_level = StorageClient.task_archive_key_from_prefix(prefix)
    if top_level in keys:
        return top_level, []
    nested = [key for key in keys if key.endswith(f"/{archive_name}")]
    if nested:
        return max(nested), []
    return None, [key for key in keys if key[len(prefix) :]]


async def _task_location(
    session, trial: TrialModel
) -> tuple[TaskModel | None, str | None, str | None]:
    """The trial's task row, and where its files are: the pinned version's
    storage key and path first, else the task's."""
    task = await session.get(TaskModel, trial.task_id)
    version = (
        await session.get(TaskVersionModel, trial.task_version_id)
        if trial.task_version_id
        else None
    )
    task_s3_key = (version.task_s3_key if version else None) or (
        task.task_s3_key if task else None
    )
    task_path = (version.task_path if version else None) or (
        task.task_path if task else None
    )
    return task, task_s3_key, task_path


async def build_attempt_spec(attempt: MonitorAttempt) -> AttemptSpec:
    if attempt.trial_id is None:
        raise ValueError("This attempt does not drive a trial")
    async with get_read_session() as session:
        trial = await session.get(TrialModel, attempt.trial_id)
        if trial is None:
            raise ValueError("Trial not found")
        task, task_s3_key, task_path = await _task_location(session, trial)
        job = await session.get(WorkerJobModel, attempt.worker_job_id)
        spec = AttemptSpec(
            trial_id=trial.id,
            worker_job_id=attempt.worker_job_id,
            worker_job_attempt=attempt.attempt,
            trial_attempt=trial.attempts,
            trial_kind=trial.kind or "agent",
            task_id=trial.task_id,
            task_name=task.name if task else trial.task_id,
            agent=trial.agent,
            model=trial.model,
            harbor_config=_with_prebuilt_image(trial.harbor_config, job),
            org_id=trial.org_id,
            billed_user_id=trial.billed_user_id,
            task_archive_url=None,
        )
        task_version_id = trial.task_version_id
    source = await presign_task_source(
        task_s3_key, task_path, task_version_id=task_version_id
    )
    spec.task_archive_url, spec.task_files, spec.task_files_version = source
    return spec


class TaskSource(NamedTuple):
    """Short-lived links to the task's files, and the name of exactly those files."""

    archive_url: str | None
    files: dict[str, str]  # relative path -> URL
    version: str | None


def task_files_version(
    task_version_id: str | None, archive_key: str | None, file_keys: list[str]
) -> str | None:
    """Names the storage objects a trial fetches, so a warm snapshot's copy of
    them is reused only for the same files. The version id alone is not enough:
    overwriting a version in place moves it to a new immutable source. None for
    a trial pinned to no version, whose files are always fetched."""
    if not task_version_id or not (archive_key or file_keys):
        return None
    raw = json.dumps([task_version_id, archive_key, sorted(file_keys)])
    return hashlib.sha256(raw.encode()).hexdigest()[:TASK_FILES_VERSION_LENGTH]


async def presign_task_source(
    task_s3_key: str | None,
    task_path: str | None,
    *,
    task_version_id: str | None = None,
) -> TaskSource:
    prefix = normalize_s3_prefix(resolve_s3_key(task_s3_key, task_path))
    if not prefix:
        return TaskSource(None, {}, None)
    storage = get_storage_client()
    archive_key, files = select_task_objects(prefix, await storage.list_keys(prefix))
    version = task_files_version(task_version_id, archive_key, files)
    if archive_key is not None:
        return TaskSource(await storage.get_presigned_url(archive_key), {}, version)
    if not files:
        return TaskSource(None, {}, None)
    urls = await storage.get_presigned_urls_batch(files)
    return TaskSource(None, {key[len(prefix) :]: urls[key] for key in files}, version)


class WarmSource(BaseModel):
    """A warm-up request plus the identity its snapshot is cached under."""

    identity: str  # task + version + prebuilt image
    request: WarmRequest


async def warm_source_for_trial(trial_id: str, job_id: str) -> WarmSource | None:
    """What a warm-up sandbox needs: where the task is, and its prebuilt image if any.
    Also returns the identity (task + version + image) the cache is keyed on."""
    async with get_read_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return None
        _, task_s3_key, task_path = await _task_location(session, trial)
        job = await session.get(WorkerJobModel, job_id)
        docker_image = (_with_prebuilt_image(trial.harbor_config, job) or {}).get(
            "docker_image"
        )
        identity = f"{trial.task_id}|{trial.task_version_id or task_s3_key}|{docker_image or ''}"
        task_id, task_version_id = trial.task_id, trial.task_version_id
    source = await presign_task_source(
        task_s3_key, task_path, task_version_id=task_version_id
    )
    if not source.archive_url and not source.files:
        return None
    return WarmSource(
        identity=identity,
        request=WarmRequest(
            task_archive_url=source.archive_url,
            task_files=source.files,
            docker_image=docker_image,
            task_id=task_id,
            task_files_version=source.version,
        ),
    )


async def acknowledge_monitor(attempt: MonitorAttempt) -> None:
    """Record the monitor's first check-in. Idempotent."""
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.id == attempt.worker_job_id,
                WorkerJobModel.attempts == attempt.attempt,
                WorkerJobModel.monitor_acked_at.is_(None),
            )
            .values(monitor_acked_at=utcnow())
        )
