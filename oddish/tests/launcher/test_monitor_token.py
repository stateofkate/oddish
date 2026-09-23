from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from oddish.db.models import WorkerJobStatus, utcnow
from oddish.workers.launcher import token as monitor_token
from oddish.workers.queue.job_tokens import hash_token, mint_token
from oddish.workers.launcher.token import MonitorGone, MonitorUnauthorized


def _job(raw: str, **overrides):
    values = {
        "id": "job-1",
        "attempts": 2,
        "status": WorkerJobStatus.RUNNING,
        "current_worker_id": "worker-1",
        "current_queue_slot": 4,
        "subject_table": "trials",
        "subject_id": "trial-1",
        "org_id": "org-1",
        "monitor_token_hash": hash_token(raw),
        "monitor_token_expires_at": utcnow() + timedelta(hours=1),
        "monitor_token_revoked_at": None,
        "job_token_hash": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_credential_round_trips_and_authorizes_the_owning_attempt():
    raw, _ = mint_token()
    credential = monitor_token.format_credential("job-1", 2, raw)
    job_id, attempt, parsed = monitor_token.parse_credential(credential)
    assert (job_id, attempt, parsed) == ("job-1", 2, raw)

    owner = monitor_token.check_credential(
        _job(raw), attempt=attempt, raw_token=parsed, now=utcnow()
    )
    assert owner == monitor_token.MonitorAttempt(
        "job-1", 2, "worker-1", trial_id="trial-1", org_id="org-1", queue_slot=4
    )


@pytest.mark.parametrize(
    "credential", ["", "job-1", "job-1.2", "job-1.x.token", ".2.t"]
)
def test_malformed_credentials_are_unauthorized(credential):
    with pytest.raises(MonitorUnauthorized):
        monitor_token.parse_credential(credential)


def test_wrong_expired_or_missing_token_is_unauthorized():
    raw, _ = mint_token()
    now = utcnow()
    with pytest.raises(MonitorUnauthorized):
        monitor_token.check_credential(_job(raw), attempt=2, raw_token="nope", now=now)
    with pytest.raises(MonitorUnauthorized):
        monitor_token.check_credential(
            _job(raw, monitor_token_expires_at=now), attempt=2, raw_token=raw, now=now
        )
    with pytest.raises(MonitorUnauthorized):
        monitor_token.check_credential(None, attempt=2, raw_token=raw, now=now)


def test_superseded_revoked_or_finished_attempt_is_gone():
    raw, _ = mint_token()
    now = utcnow()
    with pytest.raises(MonitorGone):
        monitor_token.check_credential(
            _job(raw, attempts=3), attempt=2, raw_token=raw, now=now
        )
    with pytest.raises(MonitorGone):
        monitor_token.check_credential(
            _job(raw, monitor_token_revoked_at=now), attempt=2, raw_token=raw, now=now
        )
    with pytest.raises(MonitorGone):
        monitor_token.check_credential(
            _job(raw, status=WorkerJobStatus.RETRYING),
            attempt=2,
            raw_token=raw,
            now=now,
        )


def test_the_agents_job_token_cannot_authenticate_the_monitor():
    agent_raw, agent_hash = mint_token()
    monitor_raw, _ = mint_token()
    job = _job(monitor_raw, job_token_hash=agent_hash)
    with pytest.raises(MonitorUnauthorized):
        monitor_token.check_credential(
            job, attempt=2, raw_token=agent_raw, now=utcnow()
        )


@pytest.mark.asyncio
async def test_revoke_is_scoped_to_the_attempt_it_names(monkeypatch):
    # Retries reuse the row: attempt 1's late cleanup must not revoke the
    # credential attempt 2 minted.
    statements = []

    class _Session:
        async def execute(self, statement):
            statements.append(statement)

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    monkeypatch.setattr(monitor_token, "get_session", _get_session)
    await monitor_token.revoke_monitor_credential("job-1", attempt=1)

    (statement,) = statements
    where = str(
        statement.whereclause.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "worker_jobs.attempts = 1" in where
