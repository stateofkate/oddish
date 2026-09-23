"""Settlement frees the slot and revokes the token even when a step fails.

Both run after the outcome write. A revoke that raises must not skip the
release, and a settle that died after the outcome write leaves them to the next
call, which finds the job already settled and repeats both (no-ops once done).
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TrialModel, WorkerJobModel, WorkerJobStatus
from oddish.workers.queue import monitor_settle


def _job(payload=None):
    return SimpleNamespace(
        id="job-1", kind="trial", queue_key="q", status=WorkerJobStatus.RUNNING,
        attempts=2, max_attempts=6, subject_table="trials", subject_id="task-1-0",
        payload=payload or {}, org_id="org-1", parent_job_id=None,
        current_worker_id="w-1", current_queue_slot=3, provider=None,
        external_id=None, monitor_result_accepted_at="accepted",
        monitor_result={"attempt": 2, "outcome": None},
    )  # fmt: skip


def _settle_fakes(monkeypatch, job):
    trial = SimpleNamespace(
        id="task-1-0", task_id="task-1", experiment_id=None, task_version_id=None,
        agent="oracle", model=None, environment="docker", harbor_config=None,
        kind="agent", org_id="org-1", billed_user_id=None, attempts=1,
    )  # fmt: skip
    rows = {WorkerJobModel: job, TrialModel: trial}

    @asynccontextmanager
    async def get_session():
        async def get(model, _id):
            return rows.get(model)

        yield SimpleNamespace(get=get)

    async def noop(*_args, **_kwargs):
        return None

    async def outcome_for_settled_trial(_claimed, _outcome):
        return SimpleNamespace(success=None)

    async def record_outcome(**_kwargs):
        job.status = WorkerJobStatus.FAILED  # the row leaves RUNNING
        return WorkerJobStatus.FAILED

    handler = SimpleNamespace(outcome_for_settled_trial=outcome_for_settled_trial)
    monkeypatch.setattr(monitor_settle, "get_session", get_session)
    monkeypatch.setattr(monitor_settle, "_settle_trial_attempt", noop)
    monkeypatch.setattr(
        monitor_settle, "ensure_builtin_handlers_registered", lambda: None
    )
    monkeypatch.setattr(monitor_settle, "get_handler", lambda _kind: handler)
    monkeypatch.setattr(monitor_settle, "_record_outcome", record_outcome)
    monkeypatch.setattr(monitor_settle, "_post_success_hook", None)


@pytest.mark.asyncio
async def test_a_failed_revoke_still_releases_and_a_re_settle_repeats_both(
    monkeypatch,
):
    job = _job()
    _settle_fakes(monkeypatch, job)
    revokes: list[tuple[str, int]] = []
    releases: list[tuple[str, int, str]] = []

    async def revoke(job_id, *, attempt):
        revokes.append((job_id, attempt))
        if len(revokes) == 1:
            raise ConnectionError("revoke failed once")

    async def release(*, queue_key, slot, worker_id):
        releases.append((queue_key, slot, worker_id))

    async def no_lease(**_kwargs):  # pragma: no cover - no lease recorded
        raise AssertionError("no capacity lease to release")

    monkeypatch.setattr(
        monitor_settle.monitor_token, "revoke_monitor_credential", revoke
    )
    monkeypatch.setattr(monitor_settle, "release_queue_slot", release)
    monkeypatch.setattr(monitor_settle, "release_sandbox_capacity_lease", no_lease)

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"
    assert revokes == [("job-1", 2)]
    assert releases == [("q", 3, "w-1")]  # released although the revoke raised

    again = await monitor_settle.settle_monitor_result("job-1", attempt=2)
    assert again == "already_settled"
    assert revokes == [("job-1", 2)] * 2
    assert releases == [("q", 3, "w-1")] * 2

    # A newer attempt owns the row: the old attempt's re-settle touches nothing.
    job.attempts = 3
    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == (
        "already_settled"
    )
    assert len(revokes) == 2 and len(releases) == 2


@pytest.mark.asyncio
async def test_settle_releases_the_capacity_lease_the_launcher_recorded(monkeypatch):
    job = _job({monitor_settle.CAPACITY_PAYLOAD_KEY: {"provider": "ec2", "slot": 4}})
    _settle_fakes(monkeypatch, job)
    leases: list[tuple[str, int, str]] = []

    async def revoke(job_id, *, attempt):
        return None

    async def release(*, queue_key, slot, worker_id):
        raise ConnectionError("queue slot release failed")

    async def release_lease(*, provider, slot, worker_id):
        leases.append((provider, slot, worker_id))

    monkeypatch.setattr(
        monitor_settle.monitor_token, "revoke_monitor_credential", revoke
    )
    monkeypatch.setattr(monitor_settle, "release_queue_slot", release)
    monkeypatch.setattr(
        monitor_settle, "release_sandbox_capacity_lease", release_lease
    )

    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == "settled"
    # Released with the handed-off worker's id, although the slot release raised.
    assert leases == [("ec2", 4, "w-1")]

    # A re-settle after a crash repeats it; the release no-ops once done.
    assert await monitor_settle.settle_monitor_result("job-1", attempt=2) == (
        "already_settled"
    )
    assert leases == [("ec2", 4, "w-1")] * 2
