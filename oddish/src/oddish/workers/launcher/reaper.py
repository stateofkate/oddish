"""Ask a silent monitor whether it is alive before the stale-heartbeat reap.

Runs before the reconciler's transaction, because it makes provider calls. For
each monitor-owned RUNNING job whose heartbeat is stale it records one verdict
on the row; the reap then only takes monitor-owned jobs whose verdict allows it.

Four observations, four actions:
  ALIVE         the monitor answers             -> keep, reset the count
  GONE          monitor exited / sandbox ended  -> reapable now
  UNKNOWN       the provider cannot be reached  -> keep; never inferred dead
  UNRESPONSIVE  sandbox is there, no answer     -> count it; reapable after
                                                   ``UNRESPONSIVE_SWEEPS`` in a row
A job whose result was already accepted is settled instead, never reaped.

The launcher hands off as soon as the sandbox exists and stops beating, so a
monitor that never checks in is found here: its heartbeat goes stale, the
provider is asked (no status file yet reads as GONE), and the launch deadline
bounds it whatever the provider says.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum

from sqlalchemy import text

from oddish.config import settings
from oddish.db import get_session
from oddish.workers.console import console
from oddish.workers.launcher import token as monitor_token
from oddish.workers.launcher.providers import base as monitor_sandbox
from oddish.workers.launcher.settle import settle_monitor_result

logger = logging.getLogger(__name__)

CHECK_TRIES = 3
CHECK_TIMEOUT_SECONDS = 10.0
UNRESPONSIVE_SWEEPS = 3
# Stored in worker_jobs.monitor_status_check_count.
REAPABLE = UNRESPONSIVE_SWEEPS
# One reconciler pass spends at most this on monitors, settlement included.
PASS_BATCH = 50
PASS_CONCURRENCY = 10
PASS_BUDGET_SECONDS = 180.0


class Verdict(str, Enum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"
    UNRESPONSIVE = "unresponsive"


async def observe(provider: str | None, external_id: str | None) -> Verdict:
    if not provider or not external_id:
        return Verdict.GONE  # never got a sandbox handle: nothing can be running
    try:
        starter = monitor_sandbox.get_starter(provider)
    except monitor_sandbox.MonitorSandboxError:
        return Verdict.UNKNOWN
    verdict = Verdict.UNRESPONSIVE
    for _ in range(CHECK_TRIES):
        try:
            reply = await asyncio.wait_for(
                starter.status(external_id), timeout=CHECK_TIMEOUT_SECONDS
            )
        except TimeoutError:
            verdict = Verdict.UNRESPONSIVE
            continue
        except Exception:
            logger.exception("status check could not reach provider=%s", provider)
            return Verdict.UNKNOWN
        if reply.alive:
            return Verdict.ALIVE
        # The command ran and said "not alive", or the sandbox is not there.
        return Verdict.GONE
    return verdict


def launch_deadline_seconds() -> int | None:
    """The claim-to-check-in bound, or None while the monitor path is off.
    Unset in settings, it is derived from the launch's own bounds."""
    if not settings.monitor_execution_enabled:
        return None
    return (
        settings.monitor_launch_deadline_seconds
        or monitor_sandbox.launch_deadline_default_seconds()
    )


async def check_stale_monitor_jobs(*, stale_after_minutes: int) -> dict[str, int]:
    await monitor_token.revoke_credentials_of_finished_jobs()
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, attempts, provider, external_id,
                           monitor_result_accepted_at IS NOT NULL AS has_result,
                           claimed_at < NOW() - make_interval(secs => :deadline)
                               AS past_deadline,
                           (:launch_deadline)::int IS NOT NULL
                               AND monitor_acked_at IS NULL
                               AND claimed_at
                                   < NOW() - make_interval(secs => COALESCE(:launch_deadline, 0))
                               AS past_launch_deadline
                    FROM   worker_jobs
                    WHERE  status::text = 'RUNNING'
                      AND  monitor_token_hash IS NOT NULL
                      AND  (
                          heartbeat_at IS NULL
                          OR heartbeat_at
                             < NOW() - make_interval(mins => :stale_after_minutes)
                          -- An accepted result that nobody settled (the API
                          -- crashed, or settlement is not inline): settle it.
                          OR monitor_result_accepted_at < NOW() - interval '60 seconds'
                          -- The attempt deadline the worker's timeout used to give.
                          OR claimed_at < NOW() - make_interval(secs => :deadline)
                          -- No check-in within the launch deadline: a failed
                          -- start, however fresh the heartbeat is (the launcher
                          -- beats from claim to hand-off).
                          OR ((:launch_deadline)::int IS NOT NULL
                              AND monitor_acked_at IS NULL
                              AND claimed_at
                                  < NOW() - make_interval(secs => COALESCE(:launch_deadline, 0)))
                      )
                    -- Least recently checked first, a bounded batch: a job
                    -- that keeps failing cannot starve the ones behind it.
                    ORDER  BY monitor_status_checked_at ASC NULLS FIRST, claimed_at ASC
                    LIMIT  :batch
                    """
                ),
                {
                    "batch": PASS_BATCH,
                    "stale_after_minutes": stale_after_minutes,
                    "deadline": settings.monitor_attempt_deadline_seconds,
                    "launch_deadline": launch_deadline_seconds(),
                },
            )
        ).all()

    counts: dict[str, int] = {}
    deadline = time.monotonic() + PASS_BUDGET_SECONDS
    gate = asyncio.Semaphore(PASS_CONCURRENCY)

    async def _one(row) -> None:
        async with gate:
            if time.monotonic() >= deadline:
                # Out of time for this pass. The row keeps its old check stamp,
                # so it is among the first picked next pass.
                counts["deferred"] = counts.get("deferred", 0) + 1
                return
            try:
                key = await _check_one(*row)
            except Exception:
                logger.exception("monitor check failed for job=%s", row[0])
                key = "error"
                await _stamp_checked(row[0], row[1])
            counts[key] = counts.get(key, 0) + 1

    await asyncio.gather(*(_one(row) for row in rows))
    return counts


async def _stamp_checked(job_id: str, attempt: int) -> None:
    """Move the job to the back of the rotation, whatever happened to it."""
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE worker_jobs SET monitor_status_checked_at = NOW()
                WHERE  id = :job_id AND status::text = 'RUNNING'
                  AND  attempts = :attempt
                """
            ),
            {"job_id": job_id, "attempt": attempt},
        )


async def _reread(job_id: str, attempt: int) -> tuple[bool, bool] | None:
    """(has_result, acked) for this attempt, or None once it has moved on."""
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT monitor_result_accepted_at IS NOT NULL,
                           monitor_acked_at IS NOT NULL
                    FROM   worker_jobs
                    WHERE  id = :job_id AND status::text = 'RUNNING'
                      AND  attempts = :attempt
                    """
                ),
                {"job_id": job_id, "attempt": attempt},
            )
        ).first()
    return None if row is None else (bool(row[0]), bool(row[1]))


async def _check_one(
    job_id,
    attempt,
    provider,
    external_id,
    has_result,
    past_deadline,
    past_launch_deadline=False,
) -> str:
    if not has_result and (past_deadline or past_launch_deadline):
        # The flags come from the batch select: a check-in or a result may have
        # landed since. Re-read this attempt before tearing anything down.
        fresh = await _reread(job_id, attempt)
        if fresh is None:
            return "superseded"
        has_result, acked = fresh
        if acked:
            past_launch_deadline = False
    if has_result:
        # Settlement moves artifacts, so it is not cheap: it shares the budget
        # and the rotation with the status checks.
        await _stamp_checked(job_id, attempt)
        outcome = await settle_monitor_result(job_id, attempt=attempt)
        return f"settle_{outcome}"
    if past_deadline or past_launch_deadline:
        # Out of time whatever the monitor says: stop the sandbox, then reap.
        # A launch that never produced a handle has nothing to stop.
        if provider and external_id:
            try:
                await monitor_sandbox.get_starter(provider).stop(external_id)
            except Exception:  # noqa: BLE001 - the provider timeout is the backstop
                logger.warning(
                    "could not stop sandbox for job=%s past its deadline", job_id
                )
        verdict = Verdict.GONE
    else:
        verdict = await observe(provider, external_id)
        if verdict is Verdict.GONE and not (provider and external_id):
            # No handle yet: the launch may still be resolving its image or
            # creating the sandbox. The deadlines above bound it, not this.
            verdict = Verdict.UNKNOWN
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    monitor_status_checked_at = NOW(),
                       monitor_status_check_count = CASE :verdict
                           WHEN 'alive' THEN 0
                           WHEN 'gone' THEN :reapable
                           WHEN 'unresponsive'
                               THEN COALESCE(monitor_status_check_count, 0) + 1
                           ELSE COALESCE(monitor_status_check_count, 0)
                       END
                WHERE  id = :job_id AND status::text = 'RUNNING'
                  -- the attempt this check observed, not a newer one
                  AND  attempts = :attempt
                """
            ),
            {
                "verdict": verdict.value,
                "reapable": REAPABLE,
                "job_id": job_id,
                "attempt": attempt,
            },
        )
    console.print(
        f"metric=monitor_status_check verdict={verdict.value} job_id={job_id} "
        f"provider={provider} external_id={external_id} "
        f"past_deadline={bool(past_deadline)} "
        f"past_launch_deadline={bool(past_launch_deadline)}"
    )
    return verdict.value
