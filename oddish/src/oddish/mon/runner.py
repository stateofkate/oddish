"""Wire the pieces around the existing driver. No logic of its own beyond order."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.models.environment_type import EnvironmentType

from oddish.mon import (
    artifacts,
    builder,
    env,
    hooks,
    loops,
    memstat,
    registry_login,
    resources,
    seams,
    task_fetch,
    task_image,
    teardown,
)
from oddish.mon.client import (
    AttemptGone,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    MonitorClient,
    MonitorRejected,
)
from oddish.mon.contract import AttemptSpec
from oddish.mon.exit_codes import EXIT_ACCEPTED, EXIT_STOPPED
from oddish.mon.status import MonitorPhase, write_status
from oddish.workers.harbor import live_tail

logger = logging.getLogger(__name__)

# The result is retried through an outage, or through our API's refusal (a
# 4xx other than 410/401, which a rollback or hotfix can lift), for about half
# an hour, then the monitor exits EXIT_FAILED and the entrypoint reports the
# failed start.
REPORT_RETRY_SECONDS = 5.0
REPORT_ATTEMPTS = 360

# Every way the control plane ends an attempt. ``run`` maps them to
# EXIT_STOPPED in one place, whichever phase they surface in.
_STOPPED_BY_CONTROL_PLANE = (AttemptGone, MonitorRejected, loops.StopTrial)


async def _finish_live_tail(trial_id: str) -> None:
    """Flush and stop the live tailer for ``trial_id``; never raises."""
    try:
        await live_tail.shutdown(trial_id)
    except Exception:
        logger.exception("live tail shutdown failed for trial=%s", trial_id)


@dataclass(slots=True)
class PreparedTask:
    """What ``prepare_task`` hands the driver: the attempt, its task on disk,
    the settled Harbor config and the reporter for its lifecycle events."""

    spec: AttemptSpec
    state: seams.ControlState
    driver_seams: seams.DriverSeams
    reporter: hooks.LifecycleReporter
    workdir: Path
    task_path: Path
    harbor_config: dict


@dataclass(slots=True)
class DriveResult:
    """How the driver ended: its outcome, or the error it raised (a driver
    crash is a result too, as it is for the worker), and the loop counters."""

    outcome: Any
    execution_error: str | None
    retryable: bool
    stats: loops.LoopStats


async def prepare_task(
    client: MonitorClient, spec: AttemptSpec, monitor_env: env.MonitorEnv
) -> PreparedTask:
    """After check-in: record the attempt, fetch the task, log into registries,
    and settle the Harbor config."""
    logger.info(
        "checked in: trial=%s job=%s attempt=%s agent=%s model=%s kind=%s",
        spec.trial_id,
        spec.worker_job_id,
        spec.worker_job_attempt,
        spec.agent,
        spec.model,
        spec.trial_kind,
    )
    write_status(trial_id=spec.trial_id, phase=MonitorPhase.STARTING)
    state = seams.ControlState()
    driver_seams = seams.build(client, state)

    workdir = Path(tempfile.mkdtemp(prefix="oddish-mon-"))
    task_path = await task_fetch.fetch_or_keep(spec, workdir)

    registries = registry_login.login(monitor_env.registry_auth)
    if registries:
        logger.info("registry login staged for: %s", ", ".join(registries))
    logger.info("starting the driver")

    reporter = hooks.LifecycleReporter(client)
    # Harbor's compose build inherits our environment: on a cache miss the
    # task builds with the builder its Dockerfile asks for.
    os.environ.update(builder.env_for(task_path))
    harbor_config = await task_image.reuse(task_path, spec.harbor_config)
    # The task's memory_mb keeps the meaning it had on the worker's
    # Daytona path: not a container cap; the sandbox, sized from the same
    # number, is the cap (Harbor's Docker environment would resolve the
    # default policy as a hard limit).
    harbor_config = resources.with_reservation_semantics(harbor_config)
    return PreparedTask(
        spec=spec,
        state=state,
        driver_seams=driver_seams,
        reporter=reporter,
        workdir=workdir,
        task_path=task_path,
        harbor_config=harbor_config,
    )


async def drive_with_watchers(
    client: MonitorClient, task: PreparedTask, monitor_env: env.MonitorEnv
) -> DriveResult:
    """Run the driver beside the heartbeat, control and lifecycle watchers.
    A watcher finishing first means the control plane ended the attempt:
    the driver is cancelled and ``StopTrial`` raised. Always flushes the
    live tail before returning."""
    from oddish.workers.harbor.runner import (
        is_retryable_execution_error,
        run_harbor_trial_async,
    )

    spec = task.spec
    drive = asyncio.create_task(
        run_harbor_trial_async(
            task_path=task.task_path,
            agent=spec.agent,
            jobs_dir=task.workdir / "jobs",
            model=spec.model,
            environment=EnvironmentType.DOCKER,
            hook_callback=hooks.make_hook_callback(
                client, spec, task.reporter, persist=task.driver_seams.persist
            ),
            trial_id=spec.trial_id,
            worker_job_id=spec.worker_job_id,
            harbor_config=task.harbor_config,
            org_id=spec.org_id,
            billed_user_id=spec.billed_user_id,
            extra_agent_env=monitor_env.agent_env or None,
            quota_seams=task.driver_seams.quota,
        )
    )
    stats = loops.LoopStats()
    watchers = [
        asyncio.create_task(loops.heartbeat_loop(client, stats=stats)),
        asyncio.create_task(
            loops.control_loop(client, task.state, stats=stats, trial_id=spec.trial_id)
        ),
        # A 410/401 on a lifecycle post stops the driver like the loops do,
        # whether or not Harbor lets the hook's exception through.
        asyncio.create_task(task.reporter.stopped()),
        # And one on the live tail's persist tick, which the tailer swallows.
        asyncio.create_task(loops.state_stopped(task.state)),
    ]
    try:
        done, _ = await asyncio.wait(
            {drive, *watchers}, return_when=asyncio.FIRST_COMPLETED
        )
        if drive not in done:
            stopped = next(task for task in done).exception()
            drive.cancel()
            await asyncio.gather(drive, return_exceptions=True)
            raise loops.StopTrial(str(stopped)) from stopped
        # A driver crash is a result too, as it is for the worker.
        outcome, execution_error, retryable = None, None, True
        try:
            outcome = drive.result()
        except _STOPPED_BY_CONTROL_PLANE:
            raise
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            retryable = is_retryable_execution_error(exc)
            logger.exception("the driver raised")
    finally:
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        # As the worker does: wait for the tailer's last persist tick so
        # the final transcript lines and checkpoint reach the control
        # plane before the client is closed and the result is reported.
        await _finish_live_tail(spec.trial_id)
    return DriveResult(
        outcome=outcome,
        execution_error=execution_error,
        retryable=retryable,
        stats=stats,
    )


async def report_until_accepted(
    client: MonitorClient, task: PreparedTask, result: DriveResult
) -> None:
    """Upload the artifacts and post the result, heartbeating meanwhile, until
    the control plane accepts it; the status file says "done" only then."""
    spec = task.spec
    logger.info(
        "driver finished: reward=%s error=%s; uploading and reporting",
        getattr(result.outcome, "reward", None),
        result.execution_error or getattr(result.outcome, "error", None),
    )
    logger.info("%s at driver-finished", memstat.snapshot())
    logger.info("loop summary: %s", result.stats.summary())
    log_only_dir = _attach_monitor_log(result.outcome)
    write_status(trial_id=spec.trial_id, phase=MonitorPhase.REPORTING)
    # Keep the heartbeat alive while uploading, as the worker does.
    beat = asyncio.create_task(loops.heartbeat_loop(client, stats=result.stats))
    try:
        await _deliver_result(
            client,
            result.outcome,
            result.execution_error,
            result.stats,
            task.reporter,
            retryable=result.retryable,
            log_only_dir=log_only_dir,
            delay=REPORT_RETRY_SECONDS,
            attempts=REPORT_ATTEMPTS,
        )
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    logger.info("result accepted by the control plane; done")
    write_status(trial_id=spec.trial_id, phase=MonitorPhase.DONE)


async def run() -> int:
    """Drive one attempt: EXIT_ACCEPTED, or EXIT_STOPPED when the control plane
    ends it in any phase. Anything else raises (EXIT_FAILED in ``__main__``)."""
    monitor_env = env.load()
    # Model credentials live in the process environment, as they do in the
    # worker today, because the driver reads them from there. The token does not.
    os.environ.update(monitor_env.agent_env)
    _move_vertex_ai_settings(monitor_env.agent_env)
    _drop_settings_names(monitor_env.agent_env)
    client = MonitorClient(monitor_env)
    trial_id = ""
    try:
        spec = await client.attempt()
        trial_id = spec.trial_id
        task = await prepare_task(client, spec, monitor_env)
        # The sandbox dies after the result: Harbor's teardown kills at once.
        with teardown.no_grace_period():
            result = await drive_with_watchers(client, task, monitor_env)
        await report_until_accepted(client, task, result)
        return EXIT_ACCEPTED
    except _STOPPED_BY_CONTROL_PLANE as exc:
        logger.warning("the control plane stopped the attempt: %s", exc)
        write_status(trial_id=trial_id, phase=MonitorPhase.STOPPED)
        return EXIT_STOPPED
    finally:
        await client.aclose()


def _drop_settings_names(agent_env: dict[str, str]) -> None:
    """Sealed ``ODDISH_*`` names are worker settings for this process (the
    entrypoint put them in os.environ before settings was built); the agent
    never saw them on the worker, so they stay out of its env."""
    for name in [name for name in agent_env if name.startswith("ODDISH_")]:
        del agent_env[name]


def _move_vertex_ai_settings(agent_env: dict[str, str]) -> None:
    """Hand the sealed Vertex names to the driver's Settings, not the agent.

    Settings is read at import, before the sealed env is applied, and the
    driver resolves a vertex_ai/ trial from it (vertex_ai_config), as on the
    worker. There the names are the driver's alone and the agent gets the key
    file's path, so they leave the agent env. Only such a trial has them.
    """
    names = [name for name in agent_env if name.startswith("VERTEX_AI_")]
    if not names:
        return
    from oddish.config import Settings, settings

    fresh = Settings()
    for field in (
        "vertex_ai_project_id",
        "vertex_ai_location",
        "vertex_ai_credentials_json",
        "vertex_ai_api_key",
    ):
        setattr(settings, field, getattr(fresh, field))
    for name in names:
        del agent_env[name]


def _attach_monitor_log(outcome) -> Path | None:
    """Copy the monitor's log next to the trial's own files, so it is uploaded
    with them and listed by the files API and ``oddish pull`` like the rest.

    Harbor writes one trial folder inside the job folder. The files API serves
    that folder, so a log left at the job root is stored but never shown. With
    no trial folder (a failure before Harbor created one) the job root is all
    there is. With no job folder at all (the driver raised before Harbor made
    one) the log goes into a folder of its own, which is returned so it can be
    uploaded in the job folder's place.

    The log is best effort: a copy that fails is a warning, and the result is
    reported without it (with no log at all, a finished trial must not turn
    into a failed start)."""
    job_dir = getattr(outcome, "job_dir", None)
    log_path = Path(os.environ.get("ODDISH_MON_LOG_PATH", "/var/log/oddish-mon.log"))
    if not log_path.is_file():
        return None
    try:
        if job_dir is None or not Path(job_dir).is_dir():
            fallback = Path(tempfile.mkdtemp(prefix="oddish-mon-log-"))
            shutil.copyfile(log_path, fallback / "oddish-mon.log")
            return fallback
        trial_dirs = [
            d for d in Path(job_dir).iterdir() if (d / "result.json").is_file()
        ]
        target = trial_dirs[0] if len(trial_dirs) == 1 else Path(job_dir)
        shutil.copyfile(log_path, target / "oddish-mon.log")
    except OSError as exc:
        logger.warning("could not attach the monitor log to the artifacts: %s", exc)
    return None


async def _deliver_result(
    client: MonitorClient,
    outcome,
    execution_error: str | None,
    stats: loops.LoopStats | None = None,
    reporter: hooks.LifecycleReporter | None = None,
    *,
    delay: float = REPORT_RETRY_SECONDS,
    attempts: int = REPORT_ATTEMPTS,
    retryable: bool = True,
    log_only_dir: Path | None = None,
) -> None:
    """Retry an outage or a refusal, ``attempts`` tries in all, then raise the
    last one. Our API's 410/401 (the attempt is over) is raised at once.

    The artifacts are uploaded until storage has them, then only the result
    is posted again; the route derives the archive's key from the attempt.
    Posting the result again is safe: a repeat of one already stored is
    answered as a duplicate (``oddish.workers.launcher.result``), or with 410
    once it is settled."""
    archive_uploaded = False
    for attempt in range(1, attempts + 1):
        try:
            if reporter is not None:
                # Stage events lost to an outage go first, so the trial shows
                # its real last stage before the result settles it.
                await reporter.flush()
            if not archive_uploaded:
                archive_uploaded = await artifacts.upload(
                    client, outcome, fallback_dir=log_only_dir
                )
            await artifacts.post_result(
                client,
                outcome,
                archive_uploaded=archive_uploaded,
                execution_error=execution_error,
                retryable=retryable,
                monitor_stats=stats.as_model() if stats else None,
            )
            return
        except (ControlPlaneUnavailable, ControlPlaneRefused) as exc:
            # A refusal is not a verdict: a rollback or hotfix within the
            # window still saves the finished trial.
            failure = (
                "refused" if isinstance(exc, ControlPlaneRefused) else "not delivered"
            )
            if attempt == attempts:
                # __main__ logs the traceback and exits EXIT_FAILED.
                logger.error("result %s after %d tries; giving up", failure, attempts)
                raise
            logger.warning(
                "result %s (%d/%d), retrying: %s", failure, attempt, attempts, exc
            )
            await asyncio.sleep(delay)
