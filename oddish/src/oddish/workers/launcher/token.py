"""Attempt-scoped credential for the in-sandbox monitor (oddish-mon).

Separate from the job token in ``job_tokens`` / ``model_gateway``: that one is
handed to the agent as an API key, so it cannot also prove "I am the monitor".
Only the SHA-256 hash is stored. The credential names its attempt so a token
from a superseded attempt is told to stop (gone) rather than merely refused.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update

from oddish.db import get_session
from oddish.db.models import WorkerJobModel, WorkerJobStatus, utcnow
from oddish.workers.queue.job_tokens import hash_token, mint_token

# Matches the slot lease: an attempt cannot outlive 12 h.
MONITOR_TOKEN_TTL = timedelta(hours=12)


class MonitorUnauthorized(Exception):
    """Credential is malformed, wrong, expired or revoked. Maps to HTTP 401."""


class MonitorGone(Exception):
    """Credential was valid for an attempt that no longer owns the job. 410."""


@dataclass(frozen=True)
class MonitorAttempt:
    worker_job_id: str
    attempt: int
    worker_id: str | None
    trial_id: str | None = None
    org_id: str | None = None
    queue_slot: int | None = None


def format_credential(worker_job_id: str, attempt: int, raw_token: str) -> str:
    return f"{worker_job_id}.{attempt}.{raw_token}"


def parse_credential(credential: str) -> tuple[str, int, str]:
    job_id, _, rest = (credential or "").partition(".")
    attempt_text, _, raw_token = rest.partition(".")
    if not job_id or not raw_token or not attempt_text.isdigit():
        raise MonitorUnauthorized("Malformed monitor credential")
    return job_id, int(attempt_text), raw_token


def check_credential(
    job: Any, *, attempt: int, raw_token: str, now: datetime
) -> MonitorAttempt:
    """Validate a parsed credential against its ``worker_jobs`` row."""
    if job is None or not job.monitor_token_hash:
        raise MonitorUnauthorized("Unknown monitor credential")
    if job.attempts != attempt:
        raise MonitorGone("A newer attempt owns this job")
    if not hmac.compare_digest(job.monitor_token_hash, hash_token(raw_token)):
        raise MonitorUnauthorized("Unknown monitor credential")
    if (
        job.monitor_token_revoked_at is not None
        or job.status != WorkerJobStatus.RUNNING
    ):
        raise MonitorGone("This attempt is no longer running")
    if job.monitor_token_expires_at is None or job.monitor_token_expires_at <= now:
        raise MonitorUnauthorized("Expired monitor credential")
    return MonitorAttempt(
        worker_job_id=job.id,
        attempt=attempt,
        worker_id=job.current_worker_id,
        trial_id=job.subject_id if job.subject_table == "trials" else None,
        org_id=job.org_id,
        queue_slot=job.current_queue_slot,
    )


async def mint_monitor_credential(worker_job_id: str, attempt: int) -> str:
    raw, digest = mint_token()
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.id == worker_job_id,
                WorkerJobModel.attempts == attempt,
                WorkerJobModel.status == WorkerJobStatus.RUNNING,
            )
            .values(
                monitor_token_hash=digest,
                monitor_token_expires_at=utcnow() + MONITOR_TOKEN_TTL,
                monitor_token_revoked_at=None,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Cannot issue a monitor credential for an inactive worker attempt"
            )
    return format_credential(worker_job_id, attempt, raw)


async def authorize_monitor(credential: str) -> MonitorAttempt:
    job_id, attempt, raw_token = parse_credential(credential)
    async with get_session() as session:
        job = await session.get(WorkerJobModel, job_id)
        return check_credential(job, attempt=attempt, raw_token=raw_token, now=utcnow())


async def revoke_credentials_of_finished_jobs() -> int:
    """A token only authorizes a RUNNING job, so this is bookkeeping, not a lock:
    whatever ended the job (cancel, failure, reap), its row says "revoked"."""
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.monitor_token_hash.is_not(None),
                WorkerJobModel.monitor_token_revoked_at.is_(None),
                WorkerJobModel.status != WorkerJobStatus.RUNNING,
            )
            .values(monitor_token_revoked_at=utcnow())
        )
        return result.rowcount or 0


async def revoke_monitor_credential(worker_job_id: str, *, attempt: int) -> None:
    """Scoped to ``attempt``: a retry reuses the row and mints its own credential."""
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.id == worker_job_id,
                WorkerJobModel.attempts == attempt,
                WorkerJobModel.monitor_token_hash.is_not(None),
                WorkerJobModel.monitor_token_revoked_at.is_(None),
            )
            .values(monitor_token_revoked_at=utcnow())
        )
