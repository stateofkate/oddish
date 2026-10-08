"""Per-kind ``JobHandler`` wrappers for the unified ``worker_jobs`` runner.

These are thin adapters: they delegate to the existing
``run_trial_job`` / ``run_task_expand_job`` / ``run_tag_project_job`` bodies
and translate the resulting domain state into a ``JobOutcome`` for the
runner to record. QA, audits, and analyzer reports run as trials.

Keeping the handlers in one module lets tests monkey-patch the
``get_session`` / ``run_*_job`` module globals without reaching into
the queue execution code.
"""

from __future__ import annotations

from oddish.core.harbor_artifacts import THUNDER_CAPACITY_UNAVAILABLE_CODE
from oddish.db import (
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    get_session,
)
from oddish.registry_auth import (
    RegistryAuthDecryptError,
    current_registry_credentials,
    decrypt_credentials,
)
from oddish.runtime.sandbox_lifecycle import DEFAULT_EXECUTION_LANE
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.queue.provider_failures import classify_provider_failure
from oddish.workers.queue.task_expand_handler import run_task_expand_job
from oddish.workers.queue.thunder_fallback import (
    THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
    emit_thunder_handoff_event,
    thunder_attempt_budget_fallback_provider,
    thunder_capacity_fallback_provider,
)
from oddish.workers.queue.trial_handler import run_trial_job


class WorkerJobLike:
    id: str
    attempts: int
    queue_key: str
    subject_id: str | None
    payload: dict
    worker_id: str | None
    queue_slot: int | None
    modal_function_call_id: str | None
    reroute_from_environment: str | None


def _fail_retryable(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=True)


def _fail_permanent(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=False)


# Trial kinds the in-sandbox monitor can drive today. Analysis kinds have the
# launcher modify the task copy and mint extra credentials, which the monitor
# path does not carry yet, so they stay on the worker.
_MONITOR_TRIAL_KINDS = {"agent"}


async def _monitor_blocker(trial_id: str) -> str | None:
    """Why the monitor cannot drive this trial (the route line's reason), or
    None when it can."""
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
    if trial is None:
        return "no-trial"
    kind = trial.kind or "agent"
    if kind not in _MONITOR_TRIAL_KINDS:
        return f"kind={kind}"
    from oddish.config import settings

    if (trial.environment or settings.harbor_environment) == "daytona":
        return "disabled-environment"
    if _requests_modal_gvisor(trial, settings.harbor_environment):
        return "modal-gvisor"
    # A trial runs on the provider it asked for. No starter for it: the worker.
    from oddish.workers.launcher.launch import control_plane_url
    from oddish.workers.launcher.providers.base import provider_for_environment

    if provider_for_environment(getattr(trial, "environment", None)) is None:
        return "no-monitor-provider"
    # The sandbox must be able to reach the control plane; a host with no
    # resolvable API URL keeps the trial on the worker instead of failing it.
    try:
        return None if control_plane_url() else "no-control-plane-url"
    except Exception:  # noqa: BLE001 - not configured is not an error here
        return "no-control-plane-url"


def _requests_modal_gvisor(trial, default_environment: str | None) -> bool:
    """A Modal trial whose Harbor environment kwargs set ``modal_vm_runtime``
    to false asked for Modal's default gVisor sandbox. The monitor sandbox is
    always a Modal VM, so only the worker, which starts the task sandbox
    through Harbor's Modal environment, can honor it."""
    if (trial.environment or default_environment) != "modal":
        return False
    environment = (getattr(trial, "harbor_config", None) or {}).get("environment")
    value = ((environment or {}).get("kwargs") or {}).get("modal_vm_runtime")
    if isinstance(value, str):
        return value.strip().lower() in ("false", "0", "no", "off")
    # CLI/API kwargs are JSON-coerced, so "modal_vm_runtime=0" arrives as int 0.
    return value is False or (type(value) is int and value == 0)


async def _monitor_can_drive(trial_id: str) -> bool:
    return await _monitor_blocker(trial_id) is None


async def _task_requests_gpu(trial_id: str) -> bool:
    """A GPU trial stays on the worker: Modal's VM runtime, which the monitor
    needs for Docker, refuses accelerators. The request lives in the task's
    own files, read as the launcher sizes its sandbox."""
    import shutil

    from oddish.db import storage
    from oddish.workers.harbor.runner import capture_sandbox_resources
    from oddish.workers.launcher.providers.base import provider_for_environment
    from oddish.workers.queue import trial_steps

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        provider = provider_for_environment(getattr(trial, "environment", None))
        prepared = await trial_steps.load_trial_context(session, trial)
    task_path, temp_dir, _ = await storage.resolve_task_directory(
        task_id=prepared.task_id,
        task_s3_key=prepared.task_s3_key,
        task_path=prepared.task_path,
    )
    try:
        resources = capture_sandbox_resources(
            task_path, prepared.trial_harbor_config, str(provider)
        )
        return (resources.gpu_count or 0) > 0
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def _move_gpu_trial_off_archil(trial_id: str) -> bool | None:
    """Move an Archil trial whose task requests GPUs to Modal before launch.

    Archil has no GPUs. The hosted rollout keeps GPU sweeps on Modal when the
    request says so, but a count declared only in task.toml is invisible to
    clients that send an explicit environment, so the rollout can still pick
    Archil. Returns the GPU answer when it was read (``None`` otherwise) so the
    monitor route does not read the task a second time.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        environment = getattr(trial, "environment", None) or ""
        if environment.strip().lower() != "archil":
            return None
    try:
        requests_gpu = await _task_requests_gpu(trial_id)
    except Exception:  # noqa: BLE001 - the launch path reports unreadable tasks
        return None
    if not requests_gpu:
        return False
    from sqlalchemy import func, update

    from oddish.workers.console import console

    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(
                TrialModel.id == trial_id,
                func.lower(TrialModel.environment) == "archil",
            )
            .values(environment="modal")
        )
    console.out(
        f"metric=archil_gpu_reroute trial={trial_id} target=modal",
        highlight=False,
    )
    return True


async def _monitor_routes(
    trial_id: str, attempt: int, *, requests_gpu: bool | None = None
) -> bool:
    """The sampled-rollout decision, logged once per claim with its reason."""
    from oddish.config import settings
    from oddish.workers.console import console
    from oddish.workers.launcher.route import Route, monitor_route

    route = monitor_route(
        trial_id,
        attempt,
        percent=settings.monitor_execution_sample_percent,
        fallback_after=settings.monitor_execution_fallback_after_attempts,
    )
    # The line reports where the trial actually goes.
    if route.monitor and (blocker := await _monitor_blocker(trial_id)):
        route = Route(False, blocker)
    elif route.monitor:
        try:
            if requests_gpu is None:
                requests_gpu = await _task_requests_gpu(trial_id)
            if requests_gpu:
                route = Route(False, "gpu")
        except Exception:  # noqa: BLE001 - the launcher could not read it either
            route = Route(False, "task-unreadable")
    console.out(
        f"metric=monitor_route trial={trial_id} attempt={attempt} "
        f"route={route.name} reason={route.reason}",
        highlight=False,
    )
    return route.monitor


def _trial_failure(
    message: str,
    *,
    retryable: bool,
    harbor_outcome: HarborOutcome | None,
) -> JobOutcome:
    """Carry provider-owned Harbor facts across the worker outcome boundary."""
    if harbor_outcome is None:
        return JobOutcome.fail(message, retryable=retryable)
    provider_failure = classify_provider_failure(
        message,
        http_status=harbor_outcome.http_status,
        exception_type=harbor_outcome.exception_type,
    )
    return JobOutcome.fail(
        message,
        retryable=retryable,
        retry_after_seconds=harbor_outcome.retry_after_seconds,
        exception_type=harbor_outcome.exception_type,
        request_id=harbor_outcome.request_id,
        session_id=harbor_outcome.session_id,
        provider_failure=provider_failure,
    )


class TrialJobHandler:
    kind = WorkerJobKind.TRIAL

    def default_queue_key(self, job: WorkerJobLike) -> str:
        return job.queue_key or "default"

    def validate_payload(self, payload: dict) -> dict:
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        trial_id = job.subject_id
        if not trial_id:
            raise ValueError("TRIAL worker_job missing subject_id")

        try:
            creds = decrypt_credentials((job.payload or {}).get("registry_auth_enc"))
        except RegistryAuthDecryptError as exc:
            return _fail_permanent(str(exc))
        from oddish.config import settings

        requests_gpu = await _move_gpu_trial_off_archil(trial_id)
        if settings.monitor_execution_enabled and await _monitor_routes(
            trial_id, job.attempts, requests_gpu=requests_gpu
        ):
            from oddish.workers.launcher.launch import launch_monitor_attempt

            handoff = await launch_monitor_attempt(job, creds or None)
            if handoff is not None:
                return handoff
            return await self.outcome_for_settled_trial(job, None)

        cred_token = current_registry_credentials.set(creds or None)
        try:
            harbor_outcome = await run_trial_job(
                trial_id,
                queue_key=job.queue_key,
                worker_id=job.worker_id,
                queue_slot=job.queue_slot,
                modal_function_call_id=job.modal_function_call_id,
                worker_job_id=job.id,
                worker_job_attempt=job.attempts,
                fallback_from_environment=job.reroute_from_environment,
            )
        finally:
            current_registry_credentials.reset(cred_token)
        return await self.outcome_for_settled_trial(job, harbor_outcome)

    async def outcome_for_settled_trial(
        self, job: WorkerJobLike, harbor_outcome: HarborOutcome | None
    ) -> JobOutcome:
        """The worker-job outcome implied by the trial row after settlement.

        Shared by the worker (after ``run_trial_job``) and by settlement of a
        result reported by the in-sandbox monitor.
        """
        trial_id = job.subject_id
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is None:
                return _fail_permanent(f"Trial {trial_id} vanished mid-run")
            if trial.status == TrialStatus.SUCCESS:
                return JobOutcome.ok()
            if trial.status == TrialStatus.FAILED:
                error_message = trial.error_message or f"Trial {trial_id} marked FAILED"
                return _trial_failure(
                    error_message,
                    retryable=False,
                    harbor_outcome=harbor_outcome,
                )
            environment = getattr(trial, "environment", None)
            retry_after_seconds = (
                harbor_outcome.retry_after_seconds
                if harbor_outcome is not None
                else None
            )
            fallback_provider = thunder_capacity_fallback_provider(
                environment, harbor_outcome
            )
            if fallback_provider is not None:
                return JobOutcome.reroute_to(
                    target_environment=fallback_provider,
                    target_execution_lane=DEFAULT_EXECUTION_LANE,
                    reason=THUNDER_CAPACITY_UNAVAILABLE_CODE,
                    retry_after_seconds=retry_after_seconds,
                    subject_attempt=trial.attempts,
                )
            if trial.status == TrialStatus.RETRYING:
                error_message = (
                    trial.error_message or f"Trial {trial_id} marked RETRYING"
                )
                budget_provider = thunder_attempt_budget_fallback_provider(
                    environment,
                    status=trial.status,
                    attempts=getattr(trial, "attempts", None),
                )
                if budget_provider is not None:
                    # Ordinary settlement already marked this retry; the
                    # dispatcher moves it to the fallback provider atomically
                    # and records the plain retry instead if it cannot.
                    emit_thunder_handoff_event(
                        "requested",
                        job_id=job.id,
                        trial_id=trial_id,
                        target=budget_provider,
                        handoff=THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
                        reason=f"failed_thunder_attempts={trial.attempts}",
                    )
                    return JobOutcome.reroute_to(
                        target_environment=budget_provider,
                        target_execution_lane=DEFAULT_EXECUTION_LANE,
                        reason=THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
                        retry_after_seconds=retry_after_seconds,
                        subject_attempt=trial.attempts,
                        error_message=error_message,
                    )
                return _trial_failure(
                    error_message,
                    retryable=True,
                    harbor_outcome=harbor_outcome,
                )
            return _fail_retryable(
                f"Trial {trial_id} left in non-terminal status {trial.status!r}"
            )


class TaskExpandJobHandler:
    """Adapter for the ``TASK_EXPAND`` kind.

    Unlike trial / analysis / verdict handlers (which read terminal
    domain state), task expansion reports its outcome directly via the
    ``run_task_expand_job`` return value; any raised exception becomes
    a retryable failure by default.
    """

    kind = WorkerJobKind.TASK_EXPAND

    def default_queue_key(self, job: WorkerJobLike) -> str:
        from oddish.config import settings

        return job.queue_key or settings.get_task_expand_queue_key()

    def validate_payload(self, payload: dict) -> dict:
        payload = dict(payload or {})
        if "task_id" not in payload:
            raise ValueError("TASK_EXPAND payload missing task_id")
        if "version" not in payload:
            raise ValueError("TASK_EXPAND payload missing version")
        payload["version"] = int(payload["version"])
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        payload = job.payload or {}
        task_id = payload.get("task_id") or job.subject_id
        version = payload.get("version")
        if version is None and job.subject_id and "-v" in job.subject_id:
            try:
                version = int(job.subject_id.rsplit("-v", 1)[1])
            except Exception:
                version = None
        if not task_id or version is None:
            raise ValueError("TASK_EXPAND payload missing task_id/version")

        summary = await run_task_expand_job(
            task_id=task_id,
            version=int(version),
            worker_job_id=job.id,
        )
        return JobOutcome.ok(summary if isinstance(summary, dict) else None)


class TagProjectJobHandler:
    """Adapter for the ``TAG_PROJECT`` kind.

    Recompute-from-truth: the handler returns SUCCESS as long as the
    underlying ``run_tag_project_job`` call completes; any raised
    exception becomes a retryable failure (the operation is idempotent).
    """

    kind = WorkerJobKind.TAG_PROJECT

    def default_queue_key(self, job: WorkerJobLike) -> str:
        return job.queue_key or "tag-project"

    def validate_payload(self, payload: dict) -> dict:
        payload = dict(payload or {})
        if not payload.get("scope"):
            raise ValueError("TAG_PROJECT payload missing scope")
        if not payload.get("target_id"):
            raise ValueError("TAG_PROJECT payload missing target_id")
        payload.setdefault("mode", "direct")
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        from oddish.workers.queue.tag_project_handler import run_tag_project_job

        summary = await run_tag_project_job(payload=job.payload or {})
        return JobOutcome.ok(summary if isinstance(summary, dict) else None)


__all__ = [
    "TagProjectJobHandler",
    "TaskExpandJobHandler",
    "TrialJobHandler",
]
