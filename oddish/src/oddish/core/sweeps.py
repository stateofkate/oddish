from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import AgentConfig

from oddish.config import (
    curated_model_error,
    is_geometric_model,
    is_hunyuan_model,
    is_xai_public_model,
    model_family_key,
    infer_model_provider_prefix,
    claude_model_provider,
    is_vertex_ai_claude_model,
    is_zai_model,
    is_minimax_model,
    is_moonshot_model,
    is_nop_oracle_agent,
    require_geometric_served_model_id,
    require_hunyuan_served_model_id,
    settings,
    DisabledEnvironmentError,
    require_enabled_environment,
)
from oddish.reasoning_effort import normalize_reasoning_effort
from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec


def validate_sweep_submission(submission: TaskSweepSubmission) -> None:
    if not submission.configs:
        raise HTTPException(status_code=400, detail="Must specify 'configs'")
    # Only clients that send allow_unknown_model=false opt into the catalog
    # check; released CLIs omit the field and keep today's pass-through.
    for config in submission.configs:
        if config.allow_unknown_model is not False or is_nop_oracle_agent(config.agent):
            continue
        error = curated_model_error(config.model)
        if error:
            raise HTTPException(status_code=422, detail=error)


def resolve_trial_model(spec: TrialSpec) -> str | None:
    """Provider/model admission shared by every trial launch path."""
    if spec.agent == "auto":
        family = model_family_key(spec.model)
        provider = infer_model_provider_prefix(spec.model)
        if not family:
            raise HTTPException(422, "Automatic harness selection requires a model")
        if (
            claude_model_provider(spec.model)
            or is_vertex_ai_claude_model(spec.model)
            or family.startswith("claude-")
            or is_zai_model(spec.model)
            or is_minimax_model(spec.model)
            or is_moonshot_model(spec.model)
        ):
            spec.agent = "claude-code"
        elif family.startswith("gemini-") and provider in (None, "gemini", "vertex_ai"):
            spec.agent = "gemini-cli"
        elif family.startswith(("gpt-", "o1", "o3", "o4")) and provider in (
            None, "openai", "azure"
        ):
            spec.agent = "codex"
        elif is_xai_public_model(spec.model):
            spec.agent = "grok-build"
        else:
            spec.agent = "mini-swe-agent"
    version = settings.harness_versions.get(spec.agent)
    if version and spec.agent_config is None:
        spec.agent_config = AgentConfig(kwargs={"version": version})
    elif version and spec.agent_config and spec.agent_config.import_path is None:
        spec.agent_config.kwargs.setdefault("version", version)
    # Geometric is a single-model endpoint. Reject an id it does not serve
    # at submit, before a trial row, queue slot, worker, and sandbox are
    # spent failing on it -- and before a foreign id could reach litellm as
    # ``openai/<id>``, whose default route is public OpenAI. This is the
    # chokepoint both the single and batch submit paths share.
    if is_geometric_model(spec.model):
        try:
            require_geometric_served_model_id(spec.model or "")
        except ValueError as exc:
            # Surface as a 400 with the message, matching this module's
            # convention.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported model for agent "
                    f"{spec.agent!r} / model {spec.model!r}: {exc}"
                ),
            ) from exc

    # Hunyuan: same served-model check, and only mini-swe-agent receives
    # the Hunyuan key and base URL, so any other harness would run unrouted.
    if is_hunyuan_model(spec.model):
        try:
            require_hunyuan_served_model_id(spec.model or "")
            if (spec.agent or "").strip().lower() != "mini-swe-agent":
                raise ValueError("Hunyuan models run only on mini-swe-agent")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported model for agent "
                    f"{spec.agent!r} / model {spec.model!r}: {exc}"
                ),
            ) from exc

    try:
        return settings.normalize_trial_model(spec.agent, spec.model)
    except ValueError as exc:
        # Preserve create_task's client error when validating before creation.
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _validate_allowed_environment(
    env: EnvironmentType,
    *,
    source: str,
    allowed_environments: Collection[EnvironmentType],
) -> None:
    if env not in allowed_environments:
        allowed = ", ".join(
            sorted(f"'{value.value}'" for value in allowed_environments)
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported sandbox environment in {source}: {env.value!r}. "
                f"Allowed values: {allowed}."
            ),
        )


def build_trial_specs_from_sweep(
    submission: TaskSweepSubmission,
    *,
    default_environment: EnvironmentType | None = None,
    allowed_environments: Collection[EnvironmentType] | None = None,
    existing_counts: dict[tuple[str, str | None, str | None], int] | None = None,
) -> list[TrialSpec]:
    trials: list[TrialSpec] = []
    effective_default_environment = submission.environment or default_environment
    try:
        require_enabled_environment(effective_default_environment)
    except DisabledEnvironmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if effective_default_environment and allowed_environments:
        _validate_allowed_environment(
            effective_default_environment,
            source="submission.environment",
            allowed_environments=allowed_environments,
        )

    for config in submission.configs:
        trial_environment = config.environment or effective_default_environment
        if trial_environment and allowed_environments:
            _validate_allowed_environment(
                trial_environment,
                source=f"configs[{config.agent}/{config.model or 'default'}].environment",
                allowed_environments=allowed_environments,
            )

        norm_model = resolve_trial_model(config)
        n = config.n_trials
        if existing_counts is not None:
            existing = existing_counts.get(
                (
                    config.agent,
                    norm_model,
                    normalize_reasoning_effort(
                        config.agent_config.kwargs.get("reasoning_effort")
                        if config.agent_config
                        else None
                    ),
                ),
                0,
            )
            n = max(0, config.n_trials - existing)

        for _ in range(n):
            trial_kwargs: dict = {
                "agent": config.agent,
                "model": config.model,
                "environment": trial_environment,
            }
            if config.agent_config:
                trial_kwargs["agent_config"] = config.agent_config
            trials.append(TrialSpec(**trial_kwargs))

    return trials


def build_task_submission_from_sweep(
    submission: TaskSweepSubmission,
    *,
    task_path: str,
    trials: list[TrialSpec],
) -> TaskSubmission:
    return TaskSubmission(
        task_path=task_path,
        name=submission.name,
        trials=trials,
        user=submission.user,
        priority=submission.priority,
        max_trial_attempts=submission.max_trial_attempts,
        experiment_id=submission.experiment_id,
        tags=submission.tags,
        gate_baselines=submission.gate_baselines,
        github_username=submission.github_username,
        harbor=submission.harbor,
        content_hash=submission.content_hash,
        link=submission.link,
        registry_auth=submission.registry_auth,
    )
