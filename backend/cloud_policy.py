from __future__ import annotations

from harbor.models.environment_type import EnvironmentType

from oddish.config import settings
from oddish.runtime.routing import (
    allowed_cloud_environments,
    default_cloud_environment,
)
from oddish.schemas import TaskSweepSubmission

ALLOWED_CLOUD_ENVIRONMENTS = allowed_cloud_environments()


def apply_archil_rollout_to_explicit_modal(
    submission: TaskSweepSubmission, *, request_hash: str
) -> None:
    """Let the Archil rollout split CPU sweeps that name Modal explicitly.

    Released CLIs (PyPI 0.1.12 sends ``environment=modal`` on every hosted run)
    and pipeline scripts name Modal without choosing it, and an explicit
    environment wins over the rollout default. Each explicit ``modal`` is
    replaced with this request's rollout pick (Modal or Archil). The pick stays
    explicit, so it still overrides an append's inherited provider -- re-running
    a saved Daytona task with ``--env modal`` must not inherit Daytona. Every
    other explicit provider -- notably ``ec2`` -- is kept, and so is Modal
    wherever Archil cannot serve the run (GPU, TPU, private registry pulls). A
    GPU count declared only in task.toml is invisible here; the trial handler
    moves those Archil trials to Modal before launch. Call after
    ``compute_request_hash`` so the routing bucket and idempotency fingerprint
    still come from the raw request.
    """
    if submission.environment not in (None, EnvironmentType.MODAL):
        return
    if (
        submission.harbor.environment.override_tpu is not None
        or (submission.harbor.environment.override_gpus or 0) > 0
        or submission.requires_gpu
        or submission.registry_auth
    ):
        return
    selected = get_default_cloud_environment(submission, request_hash=request_hash)
    if selected not in (EnvironmentType.MODAL, EnvironmentType.ARCHIL):
        return
    if submission.environment == EnvironmentType.MODAL:
        submission.environment = selected
    for config in submission.configs:
        if config.environment == EnvironmentType.MODAL:
            config.environment = selected


def get_default_cloud_environment(
    submission: TaskSweepSubmission | None = None,
    *,
    request_hash: str,
) -> EnvironmentType:
    # TPU is only servable by GKE, so a submission that requests one via
    # override_tpu defaults there -- mirroring the CLI's TPU auto-routing for
    # everything expressible in the request body. Resolved by NAME (like the
    # CLI) so the default is right even when this deployment's registry never
    # registered the backend; ALLOWED_CLOUD_ENVIRONMENTS still rejects it
    # loudly on a GKE-less deployment. A TPU declared only in task.toml is not
    # visible here (the task tarball never passes through the API) -- those
    # submissions must pass environment=gke explicitly.
    if (
        submission is not None
        and submission.harbor.environment.override_tpu is not None
    ):
        return EnvironmentType.GKE
    # GPU need arrives two ways: an explicit override in the request body, or
    # the CLI's ``requires_gpu`` flag for a count declared only in task.toml.
    # A private-registry pull is visible from the credentials the submission
    # carries. Both are negotiated here, against THIS deployment's registry,
    # so a Thunder-less deployment picks Modal instead of rejecting the run.
    requires_gpu = submission is not None and (
        (submission.harbor.environment.override_gpus or 0) > 0
        or submission.requires_gpu
    )
    requires_private_registry = submission is not None and bool(
        submission.registry_auth
    )
    gpu_types = submission.gpu_types if submission is not None else None
    # An exact ``gpu_type`` environment kwarg in the request (for example
    # ``oddish run --environment-kwarg gpu_type=H100``) wins over the task's
    # list, as it does at launch and in the Thunder handoff remap.
    exact_gpu_type = (
        (submission.harbor.environment.kwargs or {}).get("gpu_type")
        if submission is not None
        else None
    )
    if exact_gpu_type is not None:
        gpu_types = [str(exact_gpu_type)]
    current_default = default_cloud_environment(
        requires_gpu=requires_gpu,
        requires_private_registry=requires_private_registry,
        gpu_types=gpu_types,
    )
    if (
        current_default == EnvironmentType.MODAL
        and not requires_gpu
        and not requires_private_registry
        and not (submission is not None and submission.skip_archil)
        and int(request_hash, 16) % 100 < settings.archil_traffic_percent
    ):
        return EnvironmentType.ARCHIL
    return current_default
