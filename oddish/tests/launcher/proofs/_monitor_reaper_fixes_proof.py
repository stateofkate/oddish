"""The reaper's failed-check count and its teardowns, against a real Postgres
with scripted sandboxes — run by ``test_monitor_heartbeat_real_db.py``.

H14  a heartbeat resets the count, so a later stale spell again needs three
     failed checks before a reap;
H16  a teardown that fails after a reap is retried on later sweeps until the
     provider stops the sandbox (or says it is gone), a bounded number of
     times, while the job stays reaped;
H10  an UNKNOWN check neither adds to the count nor resets it;
F3   a launch with no sandbox handle whose launcher stopped beating is reaped
     on its first sweep; one whose launcher still beats is not checked.

Prints one ``CHECK <name>=<bool>`` line per assertion; exits 0 iff all hold.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta

import _harness  # sets the path and the null pool first
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session
from oddish.db.models import WorkerJobModel, utcnow
from oddish.workers.launcher import reaper
from oddish.workers.launcher.providers import base as monitor_sandbox
from oddish.workers.queue import cleanup, pending_teardowns
from oddish.workers.queue.runtime_status import get_queue_runtime_status
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

RUNNING, FAILED = WorkerJobStatus.RUNNING, WorkerJobStatus.FAILED
LAUNCHER = "launcher-proof"


class ScriptedSandbox:
    """A provider whose status and stop calls answer from a script."""

    def __init__(self):
        self.status_answers: list = []
        self.stop_answers: list = []
        self.stopped: list[str] = []

    async def status(self, external_id):
        answer = self.status_answers.pop(0) if self.status_answers else TimeoutError()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def stop(self, external_id):
        answer = self.stop_answers.pop(0) if self.stop_answers else None
        if isinstance(answer, BaseException):
            raise answer
        self.stopped.append(external_id)


def _provider(name: str) -> ScriptedSandbox:
    sandbox = ScriptedSandbox()
    monitor_sandbox._STARTERS[name] = lambda: sandbox
    return sandbox


async def _seed(
    provider: str | None,
    external_id: str | None,
    *,
    max_attempts=1,
    monitor=True,
    beating=False,
):
    """A monitor attempt whose heartbeat is stale: handed off, or, without a
    handle, a launch that never checked in. ``beating``: a fresh heartbeat."""
    job_id = "wj_" + uuid.uuid4().hex[:12]
    stale = utcnow() - timedelta(minutes=20)
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=job_id, kind=WorkerJobKind.TRIAL, queue_key="reaper-fixes",
                status=RUNNING, subject_table="trials",
                subject_id="tr_" + uuid.uuid4().hex[:12], payload={}, attempts=1,
                max_attempts=max_attempts, claimed_at=utcnow(),
                heartbeat_at=utcnow() if beating else stale,
                monitor_acked_at=stale if external_id else None,
                current_worker_id=LAUNCHER,
                monitor_token_hash="h" if monitor else None,
                provider=provider, external_id=external_id,
            )
        )  # fmt: skip
    return job_id


async def _row(job_id: str) -> WorkerJobModel:
    async with get_session() as session:
        return await session.get(WorkerJobModel, job_id)


async def _state(job_id: str) -> tuple:
    row = await _row(job_id)
    return row.monitor_status_check_count, row.status


async def _go_stale(job_id: str) -> None:
    async with get_session() as session:
        (await session.get(WorkerJobModel, job_id)).heartbeat_at = utcnow() - timedelta(
            minutes=20
        )


async def _check_and_reap() -> None:
    """The two monitor steps of one reconciler pass."""
    await reaper.check_stale_monitor_jobs(stale_after_minutes=15)
    async with get_session() as session:
        await cleanup._reap_stale_worker_jobs(session, stale_after_minutes=15)


async def _pending() -> dict[tuple[str, str], int]:
    stored = pending_teardowns.PendingTeardowns.model_validate(
        await get_queue_runtime_status(pending_teardowns.PENDING_TEARDOWNS)
    )
    return {(s.provider, s.external_id): s.tries for s in stored.sandboxes}


async def _no_tag_owners(_session):  # needs the backend's users table
    return 0


async def _main() -> int:
    await _harness.boot()
    checks: dict[str, bool] = {}
    hang = [TimeoutError()] * reaper.CHECK_TRIES  # every try of one check times out

    # -- H14: a heartbeat resets the count -------------------------------------
    s14 = _provider("s14")
    job = await _seed("s14", "sb-14")
    seen = []
    for _ in range(2):
        s14.status_answers = list(hang)
        await _check_and_reap()
        seen.append(await _state(job))
    await heartbeat_worker_job(job, current_worker_id=LAUNCHER)
    after_beat = await _state(job)
    await _go_stale(job)
    for _ in range(3):
        s14.status_answers = list(hang)
        await _check_and_reap()
        seen.append(await _state(job))
    print(f"INFO H14 {seen[:2]} beat={after_beat} next spell={seen[2:]}")
    checks["H14_a_heartbeat_resets_the_count"] = seen[:2] == [
        (1, RUNNING),
        (2, RUNNING),
    ] and after_beat == (None, RUNNING)
    checks["H14_the_next_stale_spell_needs_three_failed_checks"] = seen[2:] == [
        (1, RUNNING),
        (2, RUNNING),
        (3, FAILED),
    ]

    # -- H10: an UNKNOWN check neither adds nor resets ---------------------------
    s10 = _provider("s10")
    job = await _seed("s10", "sb-10")
    seen = []
    for answers in (hang, hang, [ConnectionError("provider down")], hang):
        s10.status_answers = list(answers)
        await _check_and_reap()
        seen.append(await _state(job))
    print(f"INFO H10 {seen}")
    checks["H10_unknown_neither_adds_nor_resets"] = seen == [
        (1, RUNNING),
        (2, RUNNING),
        (2, RUNNING),
        (3, FAILED),
    ]

    # -- H16: a failed teardown after a reap is retried, bounded ---------------
    s16 = _provider("s16")
    first_teardown: dict[str, bool] = {}  # the reap's own teardown, per sandbox

    async def _teardown(provider, external_id):
        return first_teardown.get(external_id, True)

    cleanup.cancel_job_by_worker = _teardown
    cleanup.sweep_orphaned_tag_owners = _no_tag_owners

    async def _sweep() -> dict:
        await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
        return dict(await _pending())

    retried = await _seed("s16", "sb-16", max_attempts=2)
    worker = await _seed("s16", "sb-worker", monitor=False)  # not a monitor job
    first_teardown.update({"sb-16": False, "sb-worker": False})
    s16.status_answers = [monitor_sandbox.StatusReply(exit_code=1)]  # GONE
    s16.stop_answers = [ConnectionError("provider down"), None]
    pending = [await _sweep() for _ in range(3)]
    row, worker_row = await _row(retried), await _row(worker)
    print(f"INFO H16 pending after each sweep {pending}; stopped {s16.stopped}")
    checks["H16_a_failed_teardown_is_retried_until_it_stops"] = (
        pending == [{("s16", "sb-16"): 0}, {("s16", "sb-16"): 1}, {}]
        and s16.stopped == ["sb-16"]
        and row.status == WorkerJobStatus.RETRYING  # reaped once, never revived
        and row.stale_reaped_at is not None
        and worker_row.status == FAILED  # a worker job's teardown is not retried
    )

    pending_teardowns.TEARDOWN_TRIES = 2
    never = await _seed("s16", "sb-never")
    first_teardown["sb-never"] = False
    s16.status_answers = [monitor_sandbox.StatusReply(exit_code=1)]
    s16.stop_answers = [ConnectionError("provider down")] * 5
    pending = [await _sweep() for _ in range(3)]
    print(f"INFO H16 bounded {pending}")
    checks["H16_retries_end_after_TEARDOWN_TRIES"] = (
        pending == [{("s16", "sb-never"): 0}, {("s16", "sb-never"): 1}, {}]
        and (await _row(never)).status == FAILED
        and "sb-never" not in s16.stopped
    )

    # -- F3: a launch whose launcher died before create -----------------------
    died = await _seed(None, None)  # no handle, no check-in, stale heartbeat
    beating = await _seed(None, None, beating=True)
    await _check_and_reap()
    died_row, beating_row = await _row(died), await _row(beating)
    print(
        f"INFO F3 died={(died_row.monitor_status_check_count, died_row.status)} "
        f"beating={(beating_row.monitor_status_checked_at, beating_row.status)}"
    )
    checks["F3_a_stale_launch_without_a_handle_is_reaped_on_its_first_sweep"] = (
        died_row.status == FAILED and died_row.stale_reaped_at is not None
    )
    checks["F3_a_beating_launch_without_a_handle_is_neither_checked_nor_reaped"] = (
        beating_row.status == RUNNING and beating_row.monitor_status_checked_at is None
    )

    for name, ok in checks.items():
        print(f"CHECK {name}={ok}")
    await _harness.dispose()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
