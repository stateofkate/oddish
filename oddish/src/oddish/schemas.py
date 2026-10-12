from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import RetryConfig as HarborRetryConfig
from harbor.models.task.config import MCPServerConfig as MCPServerSpec
from harbor.models.trial.config import (
    AgentConfig as HarborAgentConfig,
    ArtifactConfig as HarborArtifactConfig,
    EnvironmentConfig as HarborEnvironmentConfig,
    VerifierConfig as HarborVerifierConfig,
)

from oddish.analyze.models import Classification
from oddish.analyze.qa_stages import STAGES, QAStage
from oddish.config import (
    is_nop_oracle_agent,
    normalize_model_id,
    require_enabled_environment,
)
from oddish.db import (
    AnalysisStatus,
    Priority,
    TaskStatus,
    TrialOrigin,
    TrialStatus,
    VerdictStatus,
)
from oddish.registry_auth import normalize_registry_host
from oddish.runtime.ec2_policy import validate_ec2_environment_config

# =============================================================================
# Harbor Execution Config (wraps Harbor's native types)
# =============================================================================


class HarborConfig(BaseModel):
    """Structured Harbor execution config using Harbor's native types.

    Embeds Harbor's EnvironmentConfig, VerifierConfig, and ArtifactConfig
    directly so that new Harbor fields are automatically available without
    Oddish-side changes.
    """

    environment: HarborEnvironmentConfig = Field(
        default_factory=HarborEnvironmentConfig
    )
    verifier: HarborVerifierConfig = Field(default_factory=HarborVerifierConfig)
    artifacts: list[str | HarborArtifactConfig] = Field(default_factory=list)

    timeout_multiplier: float | None = Field(
        None,
        description=(
            "Global multiplier applied to all Harbor timeouts. Overrides the "
            "JobConfig default of 1.0 when set."
        ),
    )
    agent_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the agent execution timeout only.",
    )
    verifier_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the verifier timeout only.",
    )
    agent_setup_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the agent setup timeout only.",
    )
    environment_build_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the environment build timeout only.",
    )
    retry: HarborRetryConfig | None = Field(
        None,
        description=(
            "Harbor RetryConfig for trial-level retries (max_retries, wait "
            "multipliers, include/exclude exceptions). Uses Harbor's default "
            "when omitted."
        ),
    )

    docker_image: str | None = Field(
        None,
        description="Prebuilt Docker image (patched into task.toml, not a JobConfig field)",
    )
    mcp_servers: list[MCPServerSpec] | None = Field(
        None,
        description="MCP servers to make available in the task environment",
    )

    # --- Configurable Harbor source (override which Harbor runs this trial) ---
    source: str | None = Field(
        None,
        description="Harbor git source URL; None = locked default fork.",
    )
    ref: str | None = Field(
        None,
        description="Harbor ref (branch/tag/sha/PR); None = locked default commit.",
    )
    resolved_sha: str | None = Field(
        None,
        description="Server-stamped concrete commit SHA. Client-provided values are ignored.",
    )
    variant_id: str | None = Field(
        None,
        description="Server-stamped routing id: 'default' | '<registry-id>' | 'ephemeral'.",
    )


# =============================================================================
# Request Schemas
# =============================================================================


class TrialSpec(BaseModel):
    """Specification for a single trial (API input).

    ``agent`` and ``model`` identify *what* to run.  Per-trial Harbor overrides
    (env vars, kwargs, timeouts) live in the optional ``agent_config``.
    """

    agent: str = Field(
        ...,
        description="Agent name (e.g., 'claude-code', 'codex', 'gemini-cli', 'antigravity-cli')",
    )
    model: str | None = Field(
        None, description="Model name (e.g., 'claude-sonnet-4-20250514')"
    )
    timeout_minutes: int | None = Field(
        None,
        description="Deprecated. Oddish now requires timeouts to be declared in task.toml.",
    )
    environment: EnvironmentType | None = Field(
        None, description="Execution backend override"
    )
    agent_config: HarborAgentConfig | None = Field(
        None,
        description="Per-trial Harbor AgentConfig overrides (env vars, kwargs, setup timeout, etc.)",
    )

    def agent_overrides(self) -> dict[str, Any]:
        """The row owns agent/model identity, including when overrides name them."""
        return (
            self.agent_config.model_dump(
                mode="json", exclude_defaults=True, exclude={"name", "model_name"}
            )
            if self.agent_config else {}
        )

    @model_validator(mode="after")
    def validate_execution_settings(self) -> "TrialSpec":
        require_enabled_environment(self.environment)
        self.model = normalize_model_id(self.model)
        return self

    @model_validator(mode="after")
    def reject_timeout_override(self) -> "TrialSpec":
        if (
            "timeout_minutes" in self.model_fields_set
            and self.timeout_minutes is not None
        ):
            raise ValueError(
                "timeout_minutes is no longer supported. "
                "Set explicit [agent].timeout_sec, [verifier].timeout_sec "
                "(or timeout_sec on every [[verifiers]] stage), and "
                "[environment].build_timeout_sec in task.toml."
            )
        return self


class AgentModelPair(TrialSpec):
    """Specification for agent/model combination with trial count.

    Extends TrialSpec with sweep-specific fields (n_trials, concurrency).
    """

    n_trials: int = Field(
        1, ge=1, description="Number of trials for this agent/model pair"
    )
    concurrency: int | None = Field(
        None,
        ge=1,
        description="(Deprecated) Max parallel trials for this agent",
    )
    allow_unknown_model: bool | None = Field(
        None,
        description=(
            "False rejects a Fireworks/DeepSeek model id missing from the "
            "curated catalog; true sends it anyway. Omitted (older clients) "
            "skips the check."
        ),
    )


class RegistryAuth(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    registry: str = Field(
        "docker.io", description="Registry host. Defaults to Docker Hub (docker.io)."
    )
    username: str = Field(..., description="Registry username.")
    token: SecretStr = Field(..., description="Registry password or access token.")

    @model_validator(mode="before")
    @classmethod
    def redact_registry_userinfo(cls, data):
        if not isinstance(data, dict) or "registry" not in data:
            return data
        raw = str(data.get("registry") or "")
        try:
            parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        except ValueError:
            parsed = None
        if parsed and (parsed.username or parsed.password):
            data = dict(data)
            data["registry"] = "https://redacted:redacted@redacted.invalid"
            return data
        try:
            normalize_registry_host(raw)
        except ValueError as exc:
            message = str(exc)
            data = dict(data)
            if message == "registry port must be a valid numeric port":
                data["registry"] = "redacted.invalid:badport"
            elif message == "registry must be a host name without whitespace":
                data["registry"] = "redacted invalid"
            elif message == "registry must be a host name":
                data["registry"] = "https://[::1"
            else:
                data["registry"] = "redacted.invalid/path"
            return data
        return data

    @field_validator("registry")
    @classmethod
    def validate_registry(cls, value: str) -> str:
        return normalize_registry_host(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("registry_auth requires a non-empty username")
        return value

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("registry_auth requires a non-empty token")
        return value


class TrialRetryRequest(BaseModel):
    registry_auth: list[RegistryAuth] | None = Field(None)
    gate_baselines: bool = Field(
        True,
        description=(
            "If True (default), a retried LLM trial re-consults its scope's "
            "baselines (fresh per-retry decision). Set False (--no-baseline-gate) "
            "to re-run it ungated. The original run's opt-out does not persist."
        ),
    )


class TaskSubmission(BaseModel):
    """Task submission request (API input)."""

    task_path: str = Field(..., description="Path to Harbor task directory")
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_path if not provided)",
    )
    trials: list[TrialSpec] = Field(..., description="List of trials to run")
    user: str | None = Field(
        None,
        description="Submitting user (resolved server-side from auth when omitted)",
    )
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
    max_trial_attempts: int = Field(
        6,
        ge=1,
        description=(
            "Maximum Oddish worker attempts per trial, including the initial "
            "attempt. For example, 3 allows the initial run plus up to 2 retries."
        ),
    )
    experiment_id: str | None = Field(None, description="Optional experiment ID")
    tags: dict[str, str] = Field(default_factory=dict, description="Optional tags")
    gate_baselines: bool = Field(
        True,
        description=(
            "If True (default) and the global baseline gate is enabled, hold this "
            "submission's LLM trials until its nop/oracle baselines validate the "
            "task. Set False to run the LLM trials immediately without gating "
            "(baselines still run). Per submission; not inherited by retries."
        ),
    )
    github_username: str | None = Field(
        None,
        description="GitHub username to attribute this task to (recorded as metadata)",
    )
    harbor: HarborConfig = Field(
        default_factory=HarborConfig,  # type: ignore[arg-type]
        description="Harbor execution config (environment, verifier, artifacts, etc.)",
    )

    @model_validator(mode="after")
    def _validate_execution_environment(self) -> "TaskSubmission":
        require_enabled_environment(self.harbor.environment.type)
        _reject_gpu_tpu_conflict(self.harbor)
        for trial in self.trials:
            _reject_tpu_on_non_gke_environment(self.harbor, trial.environment)
            _reject_unsupported_ec2_configuration(self.harbor, trial.environment)
        return self

    content_hash: str | None = Field(
        None,
        description="Deterministic hash of task directory contents (set by CLI during upload)",
    )
    link: str | None = Field(
        None,
        description="URL to associate with this task (e.g. PR, issue, CI run)",
    )
    registry_auth: list[RegistryAuth] | None = Field(None)

    @model_validator(mode="after")
    def require_models(self):
        for trial in self.trials:
            if not is_nop_oracle_agent(trial.agent) and not trial.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self


class TaskSweepSubmission(BaseModel):
    """Convenience API for the common workflow: one task + many agent/model pairs.

    The server expands this into a normal TaskSubmission with trials for each agent/model pair.

    Examples:
        # Multiple agent/model pairs with different trial counts
        {
            "task_id": "abc123",
            "configs": [
                {"agent": "claude-code", "model": "claude-sonnet-4-5", "n_trials": 3},
                {"agent": "terminus-2", "model": "gemini-3-pro-preview", "n_trials": 5},
            ],
            "user": "alice",
            "harbor": {"verifier": {"disable": true}}
        }
    """

    task_id: str = Field(
        ...,
        description=(
            "Task ID from /tasks/upload/init and /tasks/upload/complete, or an "
            "existing task ID when append_to_task is true"
        ),
    )
    append_to_task: bool = Field(
        False,
        description=(
            "If true, append new trials to an existing task instead of creating "
            "a new task row"
        ),
    )
    add_trials: bool = Field(
        False,
        description="On append, create n_trials new trials per config instead of topping up existing counts.",
    )
    task_version_id: str | None = Field(
        None,
        description=(
            "Exact current task version required for this append. The server rejects "
            "the submission if the task's current version changed before admission."
        ),
    )
    external_request_id: str | None = Field(
        None,
        min_length=1,
        max_length=160,
        description=(
            "Durable caller-owned submission identity. Replays always return the "
            "original trials and never implicitly retry failed leaves."
        ),
    )
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_id if not provided)",
    )

    configs: list[AgentModelPair] = Field(
        ..., description="List of agent/model pairs with individual trial counts"
    )

    # Common fields
    user: str | None = Field(
        None,
        description="Submitting user (resolved server-side from auth when omitted)",
    )
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
    max_trial_attempts: int = Field(
        6,
        ge=1,
        description=(
            "Maximum Oddish worker attempts per trial, including the initial "
            "attempt. Applies to all trials created by this sweep submission."
        ),
    )
    experiment_id: str | None = Field(None, description="Optional experiment ID")
    tags: dict[str, str] = Field(default_factory=dict, description="Optional tags")
    timeout_minutes: int | None = Field(
        None,
        description="Deprecated. Oddish now requires timeouts to be declared in task.toml.",
    )
    environment: EnvironmentType | None = Field(
        None, description="Default execution backend override"
    )
    requires_gpu: bool = Field(
        False,
        description=(
            "The task's task.toml requests GPUs. The API never sees the task "
            "content, so the CLI reports this; it only informs the default "
            "environment choice when `environment` is omitted and never changes "
            "the GPU count the trial runs with."
        ),
    )
    gpu_types: list[str] | None = Field(
        None,
        description=(
            "The task.toml `[environment].gpu_types` list (acceptable GPU "
            "types; omitted means any). Reported by the CLI with `requires_gpu` "
            "so the default environment choice skips a backend that would "
            "reject the task at launch. Never changes what the trial runs with."
        ),
    )
    skip_archil: bool = Field(
        False,
        description="Keep Modal trials on Modal: opt out of the Archil rollout.",
    )
    gate_baselines: bool = Field(
        True,
        description=(
            "If True (default) and the global baseline gate is enabled, hold this "
            "submission's LLM trials until its nop/oracle baselines validate the "
            "task. Set False to run the LLM trials immediately without gating "
            "(baselines still run). Per submission; not inherited by retries."
        ),
    )
    github_username: str | None = Field(
        None,
        description="GitHub username to attribute this task to (recorded as metadata)",
    )
    github_id: str | None = Field(
        None,
        description="GitHub user id (Clerk provider_user_id) to attribute this task to; immutable across handle renames",
    )
    publish_experiment: bool | None = Field(
        None,
        description="If true, publish the experiment for public read-only access",
    )
    harbor: HarborConfig = Field(
        default_factory=HarborConfig,  # type: ignore[arg-type]
        description="Harbor execution config (environment, verifier, artifacts, etc.)",
    )

    @model_validator(mode="after")
    def _validate_execution_environment(self) -> "TaskSweepSubmission":
        require_enabled_environment(self.environment)
        require_enabled_environment(self.harbor.environment.type)
        _reject_gpu_tpu_conflict(self.harbor)
        for config in self.configs:
            resolved_environment = config.environment or self.environment
            _reject_tpu_on_non_gke_environment(self.harbor, resolved_environment)
            _reject_unsupported_ec2_configuration(self.harbor, resolved_environment)
        return self

    content_hash: str | None = Field(
        None,
        description="Deterministic hash of task directory contents (set by CLI during upload)",
    )
    use_default_version: bool = Field(
        False,
        description=(
            "On append, pin new trials to the task's current default version "
            "instead of the version the target experiment already runs. Ignored "
            "when the submission uploads task content, which sets its own version."
        ),
    )
    link: str | None = Field(
        None,
        description="URL to associate with this task (e.g. PR, issue, CI run)",
    )
    registry_auth: list[RegistryAuth] | None = Field(None)

    @field_validator("github_id", mode="before")
    @classmethod
    def _normalize_github_id(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def require_models(self):
        for config in self.configs:
            if not is_nop_oracle_agent(config.agent) and not config.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self

    @model_validator(mode="after")
    def reject_timeout_override(self) -> "TaskSweepSubmission":
        if (
            "timeout_minutes" in self.model_fields_set
            and self.timeout_minutes is not None
        ):
            raise ValueError(
                "timeout_minutes is no longer supported. "
                "Set explicit [agent].timeout_sec, [verifier].timeout_sec "
                "(or timeout_sec on every [[verifiers]] stage), and "
                "[environment].build_timeout_sec in task.toml."
            )
        return self


class ExperimentUpdateRequest(BaseModel):
    """Request to update experiment metadata."""

    name: str = Field(..., description="Experiment name")


class ExperimentCombineRequest(BaseModel):
    """Request to combine several experiments into one result experiment.

    The named source experiments are left untouched; a brand-new result
    experiment is created and the underlying data (task memberships and
    finished trials, plus their artifacts) of every source is copied into
    it.
    """

    source_experiment_ids: list[str] = Field(
        ...,
        description=(
            "IDs (or names) of the experiments to combine. At least two "
            "distinct sources are required."
        ),
    )
    name: str | None = Field(
        None,
        description=(
            "Name for the result experiment. A human-friendly name is "
            "generated when omitted."
        ),
    )
    copy_artifacts: bool = Field(
        True,
        description=(
            "When True (default) each copied trial gets its own duplicate of "
            "the source trial's S3 artifacts so the result experiment is fully "
            "independent. When False the copied trials reference the source "
            "trials' artifacts in place (cheaper, but shared storage)."
        ),
    )

    @model_validator(mode="after")
    def _validate_sources(self) -> "ExperimentCombineRequest":
        # Preserve order while dropping blanks/duplicates so the same
        # experiment can't be combined with itself into a doubled result.
        deduped = list(
            dict.fromkeys(
                stripped
                for s in self.source_experiment_ids
                if s and (stripped := s.strip())
            )
        )
        if len(deduped) < 2:
            raise ValueError(
                "source_experiment_ids must contain at least two distinct experiments"
            )
        self.source_experiment_ids = deduped
        if self.name is not None:
            self.name = self.name.strip() or None
        return self


class TrialCollectionRequest(BaseModel):
    """Request to gather existing trials into a new read-only collection."""

    name: str
    trial_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_sources(self) -> "TrialCollectionRequest":
        self.trial_ids = list(
            dict.fromkeys(s.strip() for s in self.trial_ids if s and s.strip())
        )
        self.task_ids = list(
            dict.fromkeys(s.strip() for s in self.task_ids if s and s.strip())
        )
        if not self.trial_ids and not self.task_ids:
            raise ValueError("provide at least one trial id or task id")
        return self


class QARunRequest(BaseModel):
    """Optional sandbox selection for a QA or pre-trial audit rerun."""

    environment: Literal["modal", "daytona", "unikraft"] | None = Field(
        None,
        description="QA sandbox provider. Null uses the deployed worker default.",
    )

    @model_validator(mode="after")
    def reject_disabled_environment(self) -> "QARunRequest":
        require_enabled_environment(self.environment)
        return self


class FullQARunRequest(BaseModel):
    """Rerun both QA stages for the version selected by the caller."""

    expected_version_id: str = Field(min_length=1)


class QAEvalCreateRequest(TrialSpec):
    """Replay one QA prompt version over exact historical solver trials:
    picked ones (a subset run) or every counted golden's."""

    name: str = Field(..., max_length=255)
    source_trial_ids: list[str] = Field(default_factory=list)
    goldens: bool = Field(
        False,
        description="Replay every counted golden's trial instead of source_trial_ids.",
    )
    golden_source: str | None = Field(
        None,
        max_length=128,
        description="With goldens, replay only the counted goldens from this source.",
    )
    prompt_name: str | None = Field(
        None,
        max_length=128,
        description="Required with prompt_text; prompt_sha256 uses the recorded name.",
    )
    prompt_text: str | None = Field(None, max_length=200_000)
    prompt_sha256: str | None = Field(
        None, description="Replay a recorded prompt version instead of sending text."
    )
    stage: QAStage = QAStage.POST_TRIAL
    agent: str = "claude-code"
    preflight: bool = Field(False, description="Run one selected case to validate execution.")
    preflight_run_id: str | None = Field(
        None, description="Require a successful preflight with identical inputs."
    )
    model: str | None = Field(
        None,
        description=(
            "Analysis model override. Null uses the deployed production QA model."
        ),
    )
    environment: Literal["modal", "daytona", "unikraft"] | None = Field(
        None,
        description="QA sandbox provider. Null uses the deployed worker default.",
    )
    audit_context: Literal["current", "none"] = Field(
        "current",
        description=(
            "current includes the task version's current source-audit findings; "
            "none measures historical classifier behavior without those findings"
        ),
    )

    @model_validator(mode="after")
    def _validate_qa_eval(self) -> "QAEvalCreateRequest":
        self.name = self.name.strip()
        self.prompt_name = (self.prompt_name or "").strip() or None
        self.prompt_text = (self.prompt_text or "").strip() or None
        self.prompt_sha256 = (self.prompt_sha256 or "").strip() or None
        self.source_trial_ids = list(
            dict.fromkeys(
                value.strip()
                for value in self.source_trial_ids
                if value and value.strip()
            )
        )
        if not STAGES[self.stage].replayable:
            raise ValueError(f"{self.stage.value} prompts cannot be replayed yet")
        if not self.name:
            raise ValueError("name must not be empty")
        if (self.prompt_text is None) == (self.prompt_sha256 is None):
            raise ValueError("give exactly one of prompt_text and prompt_sha256")
        if self.prompt_text is not None and self.prompt_name is None:
            raise ValueError("prompt_name is required with prompt_text")
        if self.goldens == bool(self.source_trial_ids):
            raise ValueError("give exactly one of goldens and source_trial_ids")
        self.golden_source = (self.golden_source or "").strip() or None
        if self.golden_source and not self.goldens:
            raise ValueError("golden_source requires goldens")
        if self.model is not None:
            self.model = self.model.strip() or None
        self.agent = self.agent.strip()
        if not self.agent or is_nop_oracle_agent(self.agent):
            raise ValueError("QA evaluation requires a model-backed agent")
        if self.preflight and self.preflight_run_id:
            raise ValueError("preflight and preflight_run_id are mutually exclusive")
        return self


class CollectionAddRequest(BaseModel):
    """Request to link more trials into an existing collection."""

    trial_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    from_experiment_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_sources(self) -> "CollectionAddRequest":
        self.trial_ids = list(
            dict.fromkeys(s.strip() for s in self.trial_ids if s and s.strip())
        )
        self.task_ids = list(
            dict.fromkeys(s.strip() for s in self.task_ids if s and s.strip())
        )
        self.from_experiment_ids = list(
            dict.fromkeys(
                s.strip() for s in self.from_experiment_ids if s and s.strip()
            )
        )
        if not self.trial_ids and not self.task_ids and not self.from_experiment_ids:
            raise ValueError(
                "provide at least one trial id, task id, or source experiment id"
            )
        return self


class CollectionRemoveRequest(BaseModel):
    """Request to drop trials from an existing collection."""

    trial_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_sources(self) -> "CollectionRemoveRequest":
        self.trial_ids = list(
            dict.fromkeys(s.strip() for s in self.trial_ids if s and s.strip())
        )
        self.task_ids = list(
            dict.fromkeys(s.strip() for s in self.task_ids if s and s.strip())
        )
        if not self.trial_ids and not self.task_ids:
            raise ValueError("provide at least one trial id or task id")
        return self


class CollectionRenameRequest(BaseModel):
    """Request to rename an existing collection."""

    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped


# =============================================================================
# Response Schemas
# =============================================================================


class TaskUploadInitRequest(BaseModel):
    """Request to prepare a task upload."""

    name: str = Field(..., description="Task name derived from the local directory")
    content_hash: str = Field(
        ..., description="Deterministic hash of the task directory contents"
    )
    message: str | None = Field(
        None, description="Optional description of what changed in this version"
    )
    force_new_version: bool = Field(
        False,
        description=(
            "Allocate a new task version even when the content hash matches the "
            "latest existing version. Used when callers need a fresh version "
            "stamp."
        ),
    )
    overwrite_current_version: bool = Field(
        False,
        description=(
            "Replace the selected current version in place; trials pinned to "
            "that version will resolve to the replacement content."
        ),
    )

    @model_validator(mode="after")
    def _validate_version_mode(self) -> "TaskUploadInitRequest":
        if self.force_new_version and self.overwrite_current_version:
            raise ValueError("version upload modes are mutually exclusive")
        return self


class TaskUploadCompleteRequest(BaseModel):
    """Request to finalize a direct-to-storage task upload."""

    task_id: str
    name: str
    version: int = Field(..., ge=1)
    content_hash: str = Field(
        ..., description="Deterministic hash of the uploaded task directory contents"
    )
    message: str | None = Field(
        None, description="Optional description of what changed in this version"
    )
    overwrite_current_version: bool = Field(
        False,
        description="Finalize an in-place current-version replacement.",
    )
    staging_key: str | None = Field(
        None,
        description="Server-issued staging object for an in-place replacement.",
    )
    overwrite_base_content_hash: str | None = Field(
        None,
        description=(
            "Content hash observed when an in-place replacement was initialized; "
            "used to reject stale completions."
        ),
    )

    @model_validator(mode="after")
    def _validate_overwrite_staging_key(self) -> "TaskUploadCompleteRequest":
        if self.overwrite_current_version and not self.staging_key:
            raise ValueError("staging_key is required for an in-place replacement")
        if (
            self.overwrite_current_version
            and "overwrite_base_content_hash" not in self.model_fields_set
        ):
            raise ValueError(
                "overwrite_base_content_hash is required for an in-place replacement"
            )
        return self

    register_task: bool = Field(
        False,
        description=(
            "If True, persist a TaskModel + v1 TaskVersionModel row when the "
            "task does not yet exist. Use this for upload-only flows "
            "(`oddish upload`) so the task becomes visible in the UI even "
            "without any trials. The sweep path leaves this False and "
            "continues to create the task row itself."
        ),
    )
    user: str | None = Field(
        None,
        description=(
            "Submitting user name used when `register_task=True` creates a "
            "new TaskModel. Ignored when the task already exists."
        ),
    )
    priority: Priority | None = Field(
        None,
        description=(
            "Priority used when `register_task=True` creates a new TaskModel. "
            "Defaults to LOW. Ignored when the task already exists."
        ),
    )


class UploadResponse(BaseModel):
    """Task upload response."""

    task_id: str
    name: str
    task_path: str | None = None
    s3_key: str | None = None
    version: int | None = None
    version_id: str | None = None
    existing_task: bool = False
    content_unchanged: bool = False
    content_hash: str | None = None


# How an uploaded name that is not a live task name found its task: "alias"
# (an older name of that task), "task_id" (the folder is named after the
# task's ID), or "content_hash" (the files equal that task's current version).
TaskUploadMatch = Literal["alias", "task_id", "content_hash"]


class TaskUploadInitResponse(UploadResponse):
    """Task upload preparation response."""

    upload_url: str | None = None
    upload_method: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    requires_completion: bool = False
    staging_key: str | None = None
    overwrite_base_content_hash: str | None = None
    # None for a live-name match or a new task.
    matched_by: TaskUploadMatch | None = None


class TrialQueueInfo(BaseModel):
    position: int | None = Field(
        None,
        description="1-based live queue position for queued/retrying trials in the current scheduler snapshot",
    )
    ahead: int | None = Field(
        None,
        description="Number of queued/retrying trials currently ahead of this trial",
    )
    queued_count: int = Field(
        ...,
        description="Total queued/retrying trials currently in this queue",
    )
    running_count: int = Field(
        ...,
        description="Total running trials currently in this queue",
    )
    concurrency_limit: int = Field(
        ...,
        description="Configured concurrency limit for this queue key",
    )


class TaskVersionResponse(BaseModel):
    """Response for a single task version."""

    id: str
    task_id: str
    version: int
    task_path: str
    task_s3_key: str | None = None
    content_hash: str | None = None
    message: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime
    # The pre-trial source audit for this exact snapshot: ``{"items": [...]}``
    # once one has succeeded. Status is carried separately so a version that was
    # audited and came back clean is distinguishable from one never audited.
    reported_findings: list[dict] | None = None
    pre_trial: dict | None = None
    pre_trial_status: str | None = None
    pre_trial_error: str | None = None

    model_config = {"from_attributes": True}


class TaskVersionRollup(BaseModel):
    """Aggregate fields shared by bounded and detailed version responses."""

    id: str
    version: int
    message: str | None = None
    created_at: datetime
    is_current: bool = False
    trial_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    pending_count: int = 0
    reward_sum: float = 0.0
    reward_total: int = 0
    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    billed_cost_usd: float = 0.0
    billed_trial_count: int = 0
    billed_has_estimated: bool = False
    billed_has_native: bool = False
    last_run_at: datetime | None = None


class TaskVersionSummary(TaskVersionRollup):
    """Per-version aggregates and audit metadata for the detail resource."""

    content_hash: str | None = None
    # Pre-trial source audit for this version, flattened to the items the task
    # page renders. Empty list + null status means never audited; empty list +
    # SUCCESS means audited and clean.
    retained_findings: list[dict] = Field(default_factory=list)
    pre_trial_findings: list[dict] = Field(default_factory=list)
    pre_trial_status: str | None = None
    pre_trial_error: str | None = None
    # The audit trial that produced these findings; votes on a finding anchor
    # to it. Absent on audits that predate the block_id record.
    pre_trial_trial_id: str | None = None
    # What this audit cost. Captured at write time; absent on audits that
    # predate that (analysis_costs has no version reference to recover it from).
    pre_trial_cost_usd: float | None = None
    # Direct VERSION-scope tags on this version (forward ref — UserTagRef is
    # defined below in the tag section; model_rebuild() runs after it).
    user_tags: list["UserTagRef"] = Field(default_factory=list)
    # Experiments that ran trials against THIS version (version-scoped, unlike
    # the task-level all-time list). Forward ref — TaskBrowseExperiment is
    # defined further below; the model_rebuild() below resolves it.
    experiments: list["TaskBrowseExperiment"] = Field(default_factory=list)


class TaskCostTotals(BaseModel):
    """Task-wide cost rollup across every (non-superseded) trial."""

    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    billed_cost_usd: float = 0.0
    billed_trial_count: int = 0
    billed_has_estimated: bool = False
    billed_has_native: bool = False
    total_trials: int = 0
    # QA/analysis spend for this task's trials, joined through ``trials``
    # because ``analysis_costs.task_id`` is NULL on trial-scoped QA rows.
    qa_cost_usd: float = 0.0
    # CUA / verifier LLM spend (``verifier_costs``). Never folded into
    # ``cost_usd`` or quotas. Distinct muted figure on the task tile.
    verifier_cost_usd: float = 0.0


class ExperimentCostTotals(BaseModel):
    """What an experiment's work cost, and what the experiment itself spent.

    ``cost_*`` covers every member trial -- homed in the experiment or
    gathered into it, the grid's own membership -- so a collection shows what
    the work it displays cost. ``owned_*`` covers only trials homed in the experiment
    (the page's "New spend"); it is the number that stays additive across
    experiments. ``billed_*`` is the subset of owned spend attributed to a
    registered user's quota. Token totals mirror those scopes: ``token_*`` is
    member-wide (the Cost tile's usage subline), ``owned_token_*`` home-only
    (the New spend subline), ``billed_token_*`` the billed subset of owned.
    ``total_trials`` counts all member trials.

    Served separately from the trial grid because it cannot be derived from it:
    the grid is paginated, and it is filtered to each task's current version, so
    it omits earlier versions, superseded retries and deleted trials --
    all of which were still billed. Expect this to exceed the sum of the visible
    rows. Owned spend counts what the quota sum and the admin cost breakdown
    count, so the page, the admin table and the invoice agree on one number.
    """

    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    token_count: int = 0
    token_trial_count: int = 0
    owned_cost_usd: float = 0.0
    owned_trial_count: int = 0
    owned_has_estimated: bool = False
    owned_has_native: bool = False
    owned_token_count: int = 0
    owned_token_trial_count: int = 0
    billed_cost_usd: float = 0.0
    billed_trial_count: int = 0
    billed_has_estimated: bool = False
    billed_has_native: bool = False
    billed_token_count: int = 0
    billed_token_trial_count: int = 0
    total_trials: int = 0

    excluded_cost_usd: float = 0.0
    owned_excluded_cost_usd: float = 0.0
    experiment_cost_excluded: bool = False

    # QA/analysis spend (``analysis_costs``), scoped exactly like the agent
    # figures above: ``qa_cost_usd`` over every member trial, ``owned_*`` over
    # homed trials only. Never folded into ``cost_usd`` -- the UI renders it as
    # a separate muted figure so the headline number keeps its meaning.
    qa_cost_usd: float = 0.0
    owned_qa_cost_usd: float = 0.0
    qa_has_estimated: bool = False
    # Completeness of recorded judge/analysis usage, not a promise that all
    # trials or future QA have finished. Unpriced rows are never free work.
    qa_cost_complete: bool = True
    qa_unpriced_count: int = 0
    qa_pending_count: int = 0
    owned_qa_cost_complete: bool = True
    owned_qa_unpriced_count: int = 0
    owned_qa_pending_count: int = 0
    # CUA / verifier LLM spend (``verifier_costs``). Same membership scopes as
    # QA. Never folded into ``cost_usd``, ``billed_*``, or user quotas.
    verifier_cost_usd: float = 0.0
    owned_verifier_cost_usd: float = 0.0
    verifier_has_estimated: bool = False


class ExperimentPageVerdict(BaseModel):
    verdict: str | None = None
    is_good: bool | None = None
    confidence: str | None = None


class PublicExperimentTaskRow(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    task_path: str
    github_meta: dict[str, str] | None = None
    current_version: int | None = None
    current_version_id: str | None = None
    trial_version: int | None = None
    trial_version_id: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    reward_success: int = 0
    reward_sum: float = 0.0
    reward_total: int = 0
    run_analysis: bool = False
    review_version_matches: bool | None = None
    verdict_status: VerdictStatus | None = None
    verdict: ExperimentPageVerdict | None = None
    verdict_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ExperimentTaskVerdict(ExperimentPageVerdict):
    primary_issue: str | None = None


class ExperimentTaskRow(PublicExperimentTaskRow):
    user: str
    pre_trial_status: VerdictStatus | None = None
    must_fix_count: int | None = None
    verdict: ExperimentTaskVerdict | None = None


class ExperimentPageSummary(BaseModel):
    task_count: int = 0
    trial_count: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    active: int = 0
    reward_sum: float = 0.0
    reward_total: int = 0
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    harness_error_count: int = 0
    average_score: float | None = None
    qa_accepted: int = 0
    qa_rejected: int = 0
    qa_running: int = 0
    qa_failed: int = 0


class PublicExperimentOpenResponse(BaseModel):
    experiment_id: str
    name: str
    created_at: datetime
    revision: datetime
    show_qa: bool = False
    has_active_trials: bool = False
    summary: ExperimentPageSummary | None = None
    tasks: list[PublicExperimentTaskRow] = Field(default_factory=list)
    next_created_at: datetime | None = None
    next_task_id: str | None = None


class ExperimentOpenResponse(PublicExperimentOpenResponse):
    owner: str | None = None
    link: str | None = None
    tasks: list[ExperimentTaskRow] = Field(default_factory=list)


class ExperimentTrialAnalysis(BaseModel):
    status: AnalysisStatus | None = None
    classification: str | None = None
    subtype: str | None = None
    evidence: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TrialPlayback(BaseModel):
    """Playback discovery and index state stored at ``trials.result._playback``.

    ``manifest_path`` keeps its original meaning for installed clients: a
    string advertises the verifier manifest. ``index_status`` is omitted for
    results recorded before indexing; see ``oddish.core.playback``.
    """

    manifest_path: str | None = None
    # "ready", "absent" or "invalid". Stored JSON is not re-validated here, so
    # one unexpected row cannot fail a whole experiment page.
    index_status: str | None = None
    index_path: str | None = None
    index_revision: str | None = None
    # invalid_manifest, missing_reference, unreadable_artifacts, index_write_failed
    index_error: str | None = None


class ExperimentTrialCell(BaseModel):
    id: str
    task_id: str
    task_path: str
    experiment_id: str | None = None
    task_version_id: str | None = None
    name: str
    agent: str
    model: str | None = None
    reasoning_effort: str | None = None
    provider: str
    queue_key: str
    status: TrialStatus
    attempts: int
    max_attempts: int
    harbor_stage: str | None = None
    reward: float | None = None
    # trial_bucket_label(): pass, partial, fail, scoreless, harness, skipped, other.
    outcome: str | None = None
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None
    cost_is_estimated: bool | None = None
    is_billed: bool = False
    cost_exclusion_reason: str | None = None
    playback: TrialPlayback | None = None
    has_trajectory: bool = False
    analysis: ExperimentTrialAnalysis = Field(default_factory=ExperimentTrialAnalysis)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PublicExperimentFocusResponse(BaseModel):
    revision: datetime
    task: PublicExperimentTaskRow
    trial: ExperimentTrialCell | None = None


class ExperimentFocusResponse(PublicExperimentFocusResponse):
    task: ExperimentTaskRow


class ExperimentTrialPageResponse(BaseModel):
    revision: datetime
    trials: list[ExperimentTrialCell] = Field(default_factory=list)
    next_created_at: datetime | None = None
    next_trial_id: str | None = None


class TaskDetailResponse(BaseModel):
    """Task detail bundle for ``GET /tasks/{task_id}/detail``."""

    task: "TaskStatusResponse"
    versions: list[TaskVersionSummary] = Field(default_factory=list)
    totals: TaskCostTotals = Field(default_factory=TaskCostTotals)


class VisibleWorkerJob(BaseModel):
    id: str
    kind: str
    status: str
    queue_key: str
    provider: str | None = None
    external_id: str | None = None
    subject_table: str | None = None
    subject_id: str | None = None
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class TrialResponse(BaseModel):
    id: str
    name: str
    task_id: str
    task_path: str
    task_version: int | None = None
    task_version_id: str | None = None
    # Pre-trial source audit of the exact version THIS trial ran against.
    # Populated only on the single-trial detail fetch: the grid's slim payload
    # carries hundreds of trials and must not haul findings for each one.
    pre_trial_findings: list[dict] = Field(default_factory=list)
    pre_trial_status: str | None = None
    pre_trial_error: str | None = None
    pre_trial_cost_usd: float | None = None
    experiment_id: str | None = None
    agent: str
    provider: str
    queue_key: str
    model: str | None
    reasoning_effort: str | None = None
    environment: str | None = Field(
        None,
        description="Execution sandbox environment recorded on the trial row.",
    )
    status: TrialStatus = Field(
        ...,
        description="Execution status: 'success'=completed (regardless of test result), 'failed'=execution error",
    )
    origin: TrialOrigin = Field(
        TrialOrigin.ODDISH,
        description=(
            "Where this trial was executed. 'oddish' = ran on Oddish's "
            "worker runtime (default). 'imported' = uploaded from an "
            "external Harbor run via `oddish import`."
        ),
    )
    attempts: int
    max_attempts: int
    harbor_stage: str | None
    reward: float | None = Field(
        None,
        description=(
            "Verifier score in [0, 1]: 1=full pass, 0=full fail, "
            "partial values indicate partial credit; null=no result"
        ),
    )
    error_message: str | None
    result: dict | None
    harbor_config: dict | None = Field(
        None,
        description=(
            "Harbor passthrough config (agent env/kwargs, environment "
            "resources, etc.). "
            "Surfaced for clients that need to render mode-specific UI."
        ),
    )
    harbor_sha: str | None = Field(
        None,
        description="Concrete Harbor commit SHA this trial executed against (None for legacy rows).",
    )
    harbor_source: str | None = Field(
        None,
        description="Harbor git source this trial executed against (None for legacy rows).",
    )
    kind: str = Field(
        default="agent",
        description="agent | qa | audit",
    )

    # Token usage & cost
    input_tokens: int | None = Field(
        None, description="Total input tokens (including cache hits)"
    )
    cache_tokens: int | None = Field(None, description="Cache tokens used")
    output_tokens: int | None = Field(None, description="Output tokens generated")
    total_steps: int | None = Field(
        None, description="Total agent trajectory steps, when available"
    )
    trajectory_duration_seconds: float | None = Field(
        None, description="Elapsed seconds between the first and last trajectory step"
    )
    total_tool_calls: int | None = Field(
        None, description="Total tool calls in the agent trajectory"
    )
    tool_counts: dict[str, int] | None = Field(
        None, description="Tool-call counts keyed by trajectory function name"
    )
    cost_usd: float | None = Field(
        None,
        description=(
            "Trial cost in USD. Native value from the agent runtime when "
            "available; otherwise estimated from token counts and a static "
            "model pricing table (see ``cost_is_estimated``)."
        ),
    )
    cost_is_estimated: bool | None = Field(
        None,
        description=(
            "True when ``cost_usd`` was derived from the static model "
            "pricing table because the agent runtime did not report a "
            "usable native cost. False when the cost came directly from "
            "the runtime. Null when no cost is available."
        ),
    )
    is_billed: bool = Field(
        False,
        description=(
            "True when the trial is attributed to a billed user "
            "(``billed_user_id`` is set), i.e. its cost counts toward "
            "billed spend and quota usage."
        ),
    )
    cost_exclusion_reason: str | None = Field(
        None,
        description=(
            "Why this trial's ``cost_usd`` is not real spend: ``key`` for a "
            "preserved provider-key exclusion, ``model`` for a cost-excluded "
            "model, or ``experiment`` when its home experiment is excluded. The cost "
            "is still reported -- the work ran -- but it is absent from the "
            "admin cost dashboards and from quota enforcement, and the UI "
            "labels it. Null means the spend is real, OR that the builder "
            "did not resolve exclusions; only callers that pass an "
            "exclusions snapshot populate it."
        ),
    )
    # QA/analysis spend for this trial. None when no QA ran -- distinct from
    # 0.0, so the UI can render nothing rather than "+$0.00 QA". None also
    # means "not resolved by this caller": most builders never populate it.
    qa_cost_usd: float | None = None
    # CUA / verifier LLM spend for this trial (``verifier_costs``). Same
    # None-vs-0.0 semantics as ``qa_cost_usd``. Never included in ``cost_usd``.
    verifier_cost_usd: float | None = None

    # Per-phase timing breakdown
    phase_timing: dict | None = Field(
        None,
        description="Per-phase duration breakdown: {environment_setup, agent_setup, agent_execution, verifier}",
    )

    # Trajectory
    playback: TrialPlayback | None = None
    has_trajectory: bool = Field(
        False,
        description="Whether this trial has a readable ATIF trajectory JSON object",
    )

    analysis_status: AnalysisStatus | None = None
    analysis: dict | None = Field(
        None,
        description="Trial analysis with classification (GOOD_SUCCESS, BAD_FAILURE, etc.), subtype, and recommendation",
    )
    analysis_error: str | None = Field(
        None,
        description="Error message if analysis failed",
    )
    analysis_started_at: datetime | None = Field(
        None,
        description="When the current analysis run started; None until a worker picks it up",
    )
    analysis_finished_at: datetime | None = Field(
        None,
        description="When the analysis reached a terminal state",
    )
    superseded_by_trial_id: str | None = Field(
        None,
        description=(
            "Set when this trial has been replaced by a user-driven "
            "retry that spawned a brand-new immutable trial. Default "
            "list/aggregate endpoints filter superseded rows out; this "
            "field lets the UI navigate the rerun chain when surfacing "
            "history."
        ),
    )
    jobs: list[VisibleWorkerJob] = Field(
        default_factory=list,
        description="Active/recent worker_jobs rows for this trial",
    )
    queue_info: TrialQueueInfo | None = Field(
        None,
        description="Live queue snapshot for queued/retrying trials",
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class UserTagRef(BaseModel):
    """Effective tag on a task, surfaced to API/CLI/frontend.

    ``current=True`` -> tag is on the selected default version (primary chip).
    ``older=True``   -> tag exists only on older versions (de-emphasized).
    """

    tag_id: str
    key: str
    value: str | None = None
    color: str | None = None
    visibility: str = "PRIVATE"
    current: bool = False
    older: bool = False


class TrialStatusQueryRequest(BaseModel):
    trial_ids: list[str] = Field(min_length=1, max_length=500)


class TrialStatusItem(BaseModel):
    id: str
    task_id: str
    task_version_id: str | None = None
    status: TrialStatus
    error_message: str | None = None
    has_trajectory: bool | None = None
    superseded_by_trial_id: str | None = None
    updated_at: datetime
    finished_at: datetime | None = None


class TrialStatusQueryResponse(BaseModel):
    trials: list[TrialStatusItem]
    missing_trial_ids: list[str] = Field(default_factory=list)


class TrialBatchCancelRequest(BaseModel):
    trial_ids: list[str] = Field(min_length=1, max_length=500)


class SweepTrialRef(BaseModel):
    id: str
    task_version_id: str | None = None


class TaskResponse(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    trials_count: int
    providers: dict[str, int]  # provider -> count of trials
    experiment_id: str | None = None
    experiment_name: str | None = None
    created_at: datetime
    new_trial_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of the trials created by this sweep submission. "
            "For append-mode submissions, this contains only the newly appended "
            "trials (not any pre-existing trials on the task). Clients can use "
            "this to filter status/watch views to only the trials they just "
            "submitted."
        ),
    )
    new_trials: list[SweepTrialRef] = Field(
        default_factory=list,
        description="Newly-created trial identities with their exact task-version pins.",
    )
    user_tags: list[UserTagRef] = Field(default_factory=list)


class TaskBatchDeleteRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=500)
    dry_run: bool = Field(
        default=True, description="Report found/missing IDs without changing anything"
    )


class TaskAliasesRequest(BaseModel):
    aliases: dict[str, str] = Field(
        min_length=1, max_length=500, description="Alias name -> task ID"
    )
    dry_run: bool = Field(
        default=True, description="Report what would change without changing anything"
    )


class TaskBatchCancelRequest(BaseModel):
    task_ids: list[str] = Field(
        default_factory=list,
        description="Task IDs to cancel in one request",
    )
    experiment_id: str | None = Field(
        default=None,
        description=(
            "When set, only cancel trials that belong to this experiment. "
            "Omitting it cancels every in-flight trial for the listed tasks."
        ),
    )


class BackfillQARequest(BaseModel):
    force: bool = False
    trial_ids: list[str] | None = None


def _reject_gpu_tpu_conflict(harbor: HarborConfig) -> None:
    """No single backend serves both accelerator families (the CLI rejects the
    combination pre-submit; this guards raw API submissions with the same 422
    instead of a runtime accelerator error)."""
    env = harbor.environment
    if env.override_tpu is not None and (env.override_gpus or 0) > 0:
        raise ValueError(
            "A submission cannot request both GPU and TPU resources: no single "
            "execution backend provides both. Drop override_gpus or override_tpu."
        )


def _reject_tpu_on_non_gke_environment(
    harbor: HarborConfig, environment: "EnvironmentType | None"
) -> None:
    """A TPU request with an EXPLICIT non-GKE environment can never run; reject
    at submit rather than minutes later at the worker's fast-fail. An unset
    environment stays permitted here -- the server resolves it (default or
    append-inheritance) and the sweep gate re-checks the resolved value."""
    if (
        harbor.environment.override_tpu is not None
        and environment is not None
        and environment != EnvironmentType.GKE
    ):
        raise ValueError(
            f"TPU requests require environment=gke; got environment="
            f"'{environment.value}'. Drop override_tpu or submit with "
            f"environment=gke."
        )


def _reject_unsupported_ec2_configuration(
    harbor: HarborConfig, environment: "EnvironmentType | None"
) -> None:
    if environment != EnvironmentType.EC2:
        return
    validate_ec2_environment_config(harbor.environment)


class TaskSweepBatchRequest(BaseModel):
    """Submit several task-sweep submissions in a single request.

    Each submission is processed independently (best-effort): a failure in one
    item neither aborts the batch nor rolls back items that already succeeded.
    The response carries a per-item status array indexed to ``submissions``.
    """

    submissions: list[TaskSweepSubmission] = Field(
        ...,
        description="Task-sweep submissions to create; each is processed independently.",
    )


class TaskSweepBatchItemResult(BaseModel):
    """Outcome of one submission within a batch sweep, keyed by request order."""

    index: int = Field(
        ...,
        description="0-based position of this item in the request's submissions array.",
    )
    success: bool = Field(..., description="True when the submission was created.")
    status_code: int = Field(
        200,
        description=(
            "Per-item outcome code: 200 on success, otherwise the failure's "
            "HTTP-equivalent status (e.g. 404 for a missing task)."
        ),
    )
    task: TaskResponse | None = Field(
        None,
        description="The created/appended task on success; null when the item failed.",
    )
    error: str | None = Field(
        None,
        description="Human-readable failure detail; null on success.",
    )


class TaskSweepBatchResponse(BaseModel):
    """Per-item results for a batch sweep submission.

    ``results`` mirrors the request's ``submissions`` order via each item's
    ``index``. Callers must inspect per-item ``success``/``status_code`` rather
    than relying solely on the top-level HTTP status (200 = all succeeded,
    207 Multi-Status = at least one item failed).
    """

    total: int = Field(..., description="Number of submissions in the request.")
    succeeded: int = Field(..., description="Count of submissions that were created.")
    failed: int = Field(..., description="Count of submissions that failed.")
    results: list[TaskSweepBatchItemResult] = Field(
        default_factory=list,
        description="Per-item outcomes, ordered by request index.",
    )


class ExperimentUpdateResponse(BaseModel):
    id: str
    name: str


class ExperimentCombineResponse(BaseModel):
    """Result of combining several experiments."""

    id: str = Field(..., description="ID of the newly created result experiment")
    name: str = Field(..., description="Name of the result experiment")
    source_experiment_ids: list[str] = Field(
        ..., description="Resolved IDs of the experiments that were combined"
    )
    tasks_linked: int = Field(
        0, description="Distinct tasks linked into the result experiment"
    )
    trials_copied: int = Field(
        0, description="Finished trials copied into the result experiment"
    )
    trials_skipped: int = Field(
        0,
        description=(
            "Source trials skipped because they were not finished "
            "(still pending/queued/running/paused) at combine time"
        ),
    )
    artifacts_copied: int = Field(
        0, description="S3 objects duplicated for the copied trials"
    )


class TrialCollectionResponse(BaseModel):
    """Result of gathering trials into a new read-only collection."""

    id: str
    name: str
    trials_linked: int
    tasks_linked: int
    trials_from_tasks: int = 0
    tasks_skipped_empty: int = 0


class QAEvalTrialResponse(BaseModel):
    source_trial_id: str
    qa_eval_trial_id: str


class QAEvalCreateResponse(BaseModel):
    experiment_id: str
    experiment_name: str
    prompt_name: str
    prompt_sha256: str
    model: str
    agent: str = "claude-code"
    preflight: bool = False
    trials: list[QAEvalTrialResponse]
    # With ``goldens``: golden trials that can no longer be replayed, each as
    # "trial id: reason".
    skipped: list[str] = Field(default_factory=list)


# How one replay did against its golden; ``off``: its trial has no counted
# golden.
QAEvalOutcome = Literal["right", "wrong", "invalid", "errored", "pending", "off"]
# Each case's one group: wrong (wrong or invalid), right, not_judged (errored
# or pending) or no_golden (off).
QAEvalGroup = Literal["wrong", "right", "not_judged", "no_golden"]
# counted: reviewed, task version unchanged since labeling (only these score
# runs); stale: the version's content changed; waiting: the rest.
QAGoldenState = Literal["counted", "waiting", "stale"]
QAGoldenReview = Literal["waiting", "stale"]
# A queued trial's group: labeled when its golden counts, else by reward.
QAGoldenQueueGroup = Literal["failed", "passed", "labeled"]


class QAEvalRunSummary(BaseModel):
    """One replay experiment scored against the counted goldens."""

    experiment_id: str
    experiment_name: str
    # The prompt version's name, else the run's prompt_name, else its hash.
    title: str
    created_at: datetime
    model: str | None
    # One name per model, whatever route it ran through.
    model_name: str
    agent: str = "claude-code"
    reasoning_effort: str | None = None
    configuration_sha256: str | None = None
    golden_set_sha256: str | None = None
    preflight: bool = False
    preflight_passed: bool = False
    prompt_sha256: str
    # Replayed picked trials, so its accuracy is over those alone.
    subset: bool
    # Full and scored: charted, and eligible for the frontier and models.
    charted: bool
    replays: int
    # The counts below are over the replays whose trial has a counted golden.
    with_golden: int
    # Scored or invalid output: accuracy's denominator.
    judged: int
    correct: int
    errored: int
    # All unfinished replays, even when their golden does not count.
    pending: int
    # correct / judged; null when nothing was judged.
    accuracy: float | None
    cost_usd: float
    # cost_usd per replay with a recorded cost; null when none has one.
    cost_per_trial: float | None
    # Finished replays with no recorded cost.
    cost_missing: int


class QALabelCount(BaseModel):
    label: str
    count: int


class QAGoldensSummary(BaseModel):
    """Live goldens whose subject still exists, each in one state."""

    counted: int
    waiting: int
    stale: int
    # Counted goldens per StageSpec.label, largest first.
    labels: list[QALabelCount]
    # Counted goldens per source (the import they came from), largest first.
    sources: list[QALabelCount] = Field(default_factory=list)


class QAEvalModelSpend(BaseModel):
    model_name: str
    cost_usd: float


class QAEvalSpend(BaseModel):
    """Spend over every run of the stage."""

    total_usd: float
    trials: int
    cost_missing: int
    # Largest first.
    by_model: list[QAEvalModelSpend]


class QAEvalModelRow(BaseModel):
    model_name: str
    # Charted runs of this model.
    runs: int
    latest_run_id: str


class QAPromptVersion(BaseModel):
    """A recorded prompt, the shipped prompt, or a hash seen only on runs."""

    sha256: str
    name: str
    # The git author, else whoever first sent the text.
    author: str | None
    first_seen_at: datetime
    git_commit: str | None
    text_available: bool
    is_production: bool
    # The closest older version with text: the default diff base.
    previous_sha256: str | None
    # Runs with a golden trial, subset runs included; the latest full one.
    runs: int
    latest_run_id: str | None


class QAEvalStatusResponse(BaseModel):
    execution_configurable: bool = True
    replayable: bool
    production_prompt_sha256: str
    goldens: QAGoldensSummary
    # Cost per trial over every run: the New run estimate.
    cost_per_trial: float | None
    spend: QAEvalSpend
    # Oldest first.
    runs: list[QAEvalRunSummary]
    # The newest charted run of the shipped prompt.
    production_run_id: str | None
    # Charted runs with a cost that no cheaper run matches on accuracy,
    # cheapest first.
    frontier: list[str]
    # Each model's latest charted run, most accurate first, then cheapest;
    # a model with no cost ranks after one with a cost.
    models: list[QAEvalModelRow]
    # Oldest first.
    versions: list[QAPromptVersion]


class QAPromptVersionText(BaseModel):
    sha256: str
    text: str


class QAEvalCase(BaseModel):
    qa_eval_trial_id: str
    task_id: str
    source_trial_id: str
    # The trial goldens key on: the source trial, or the original it copies.
    original_trial_id: str
    group: QAEvalGroup
    outcome: QAEvalOutcome
    golden_id: str | None
    # The golden's StageSpec.label and QA's answer.
    expected: str | None
    got: str | None
    cost_usd: float | None
    # For invalid and errored replays.
    error: str | None


class QAEvalErrorGroup(BaseModel):
    """Errored replays sharing a first line, file paths masked."""

    message: str
    count: int


class QAEvalRunResponse(BaseModel):
    summary: QAEvalRunSummary
    # Largest first.
    error_groups: list[QAEvalErrorGroup]
    cases: list[QAEvalCase]


class QAGoldenCreate(BaseModel):
    """One unreviewed golden, as an import adds it. The subject is
    ``source_trial_id`` for a trial stage, ``task_version_id`` otherwise."""

    stage: QAStage
    # Where the label came from, e.g. the imported file's name.
    source: str | None = Field(None, max_length=128)
    source_trial_id: str | None = None
    task_version_id: str | None = None
    expected: dict[str, Any]
    note: str = Field("", max_length=20_000)

    @model_validator(mode="after")
    def _validate_golden(self) -> "QAGoldenCreate":
        spec = STAGES[self.stage]
        self.source = (self.source or "").strip() or None
        self.expected = spec.parse_expected(self.expected)
        by_trial = spec.subject == "trial"
        if by_trial != bool(self.source_trial_id) or by_trial == bool(
            self.task_version_id
        ):
            field = "source_trial_id" if by_trial else "task_version_id"
            raise ValueError(f"{self.stage.value} goldens need {field} alone")
        return self


class QAGoldenLabel(BaseModel):
    """A solver trial's correct post-trial classification."""

    classification: Classification
    # None keeps an unchanged label's note; a changed label starts empty.
    note: str | None = Field(None, max_length=20_000)


class QAGoldenRef(BaseModel):
    id: str
    expected: dict[str, Any]
    state: QAGoldenState

    model_config = {"from_attributes": True}


class QAGoldenResponse(QAGoldenRef):
    source: str | None
    source_trial_id: str | None
    task_version_id: str
    note: str


class QAGoldenQueueTrial(BaseModel):
    trial_id: str
    task_id: str
    task_name: str
    reward: float | None
    group: QAGoldenQueueGroup
    # Production QA's classification, taxonomy subtype and root cause.
    qa_classification: str | None
    qa_subtype: str | None
    qa_reason: str | None
    golden: QAGoldenRef | None


class QAGoldenQueue(BaseModel):
    """Finished solver trials to label: an experiment's, or those of the
    goldens waiting for review or stale."""

    # The experiment's name, or "Waiting for review" / "Stale".
    title: str
    # Failed first, then by task name.
    trials: list[QAGoldenQueueTrial]


class CollectionMutationResponse(BaseModel):
    """Result of editing an existing read-only collection in place."""

    id: str
    name: str
    trials_added: int = 0
    trials_removed: int = 0
    trials_total: int = 0
    tasks_linked: int = 0
    tasks_unlinked: int = 0
    # Ids the caller named that were not members; ignored, not an error.
    trials_skipped: int = 0


class TaskBrowseExperiment(BaseModel):
    id: str
    name: str


# Deferred rebuild: resolves TaskVersionSummary's forward refs to UserTagRef
# and TaskBrowseExperiment now that both are defined.
TaskVersionSummary.model_rebuild()


class TaskBrowseTrial(BaseModel):
    id: str
    name: str
    status: TrialStatus
    reward: float | None = None
    error_message: str | None = None
    agent: str = ""
    model: str | None = None
    reasoning_effort: str | None = None


class TaskBrowseDelivery(BaseModel):
    """A historical delivery record or an active batch membership.

    ``source`` says where the record came from: ``history`` is an imported
    row in ``task_delivery_history`` (the delivery-metadata backfill, quoted
    from spreadsheets and trackers), ``delivery`` is a finalized Oddish
    delivery that contains the task. ``batch`` is the source's batch name or
    the delivery's name; ``date`` is verbatim from the source for history
    rows (not normalized) and the finalize date for deliveries.
    """

    customer: str
    batch: str | None = None
    date: str | None = None
    source: Literal["history", "delivery"]
    # Active memberships appear only in TaskBrowseItem.active_deliveries.
    delivery_id: str | None = None
    status: Literal["active", "finalized"] | None = None


RunCoverage = Literal[
    "any", "no_trials", "validation_only", "model_trials", "scored_model_trials",
    "no_model_runs", "in_progress", "finished", "unknown",
]


class TaskRunGroup(BaseModel):
    total: int = 0
    scored: int = 0
    reward_sum: float = 0.0
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    harness_count: int = 0
    skipped_count: int = 0
    scoreless_count: int = 0
    other_count: int = 0
    last_activity: datetime | None = None


class TaskRunSummary(BaseModel):
    model: TaskRunGroup = Field(default_factory=TaskRunGroup)
    oracle: TaskRunGroup = Field(default_factory=TaskRunGroup)
    nop: TaskRunGroup = Field(default_factory=TaskRunGroup)


class TaskCostSummary(BaseModel):
    completed_trials: int = 0
    trial_sum: float = 0
    trial_count: int = 0
    qa_sum: float = 0
    qa_jobs: int = 0
    qa_unpriced_jobs: int = 0
    qa_unattributed_jobs: int = 0


class TaskEffortSummary(BaseModel):
    """Recorded effort of completed model trials on the selected version.

    Token totals require both input and output; cache tokens are already part
    of input. Each measurement has its own denominator; missing is not zero.
    """

    cost: TaskCostSummary | None = None
    completed_trials: int = Field(default=0, ge=0)
    steps_sum: int = Field(default=0, ge=0)
    steps_trial_count: int = Field(default=0, ge=0)
    tokens_sum: int = Field(default=0, ge=0)
    tokens_trial_count: int = Field(default=0, ge=0)
    uncached_input_tokens_sum: int = Field(default=0, ge=0)
    uncached_input_tokens_trial_count: int = Field(default=0, ge=0)
    cached_input_tokens_sum: int = Field(default=0, ge=0)
    cached_input_tokens_trial_count: int = Field(default=0, ge=0)
    output_tokens_sum: int = Field(default=0, ge=0)
    output_tokens_trial_count: int = Field(default=0, ge=0)


class ExperimentEffortSummary(TaskEffortSummary):
    """Completed model effort within an experiment, with recorded cost coverage."""

    cost_sum: float = Field(default=0, ge=0)
    cost_trial_count: int = Field(default=0, ge=0)


class TaskBrowseItem(BaseModel):
    effort: TaskEffortSummary = Field(default_factory=TaskEffortSummary)
    sort_value: float | None = None
    created_at: datetime | None = None
    run_summary: TaskRunSummary | None = None
    qa_outcome: Literal["accepted", "rejected", "outdated", "unreviewed", "running", "failed"] = "unreviewed"
    id: str
    name: str
    current_version: int | None = None
    current_version_id: str | None = None
    version_count: int
    total_trials: int
    completed_trials: int
    failed_trials: int
    reward_success: int
    reward_sum: float
    reward_total: int
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    harness_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    # Trajectory-length distribution and distinct-harness count over the
    # same scoped trials, from the summary row. Percentiles are None when
    # no trial recorded a step count.
    steps_present: int = 0
    steps_p25: int | None = None
    steps_p50: int | None = None
    steps_p75: int | None = None
    agent_count: int = 0
    # True when the task matches the caller's ``pin_author`` (the browser's
    # "mine first"); those rows sort ahead of the rest of the page order.
    author_pinned: bool = False
    # Every customer this task is recorded as having been sent to, oldest
    # source first. Empty means no record, which is not proof it was never
    # sent: history coverage is partial (see the backfill docs).
    deliveries: list[TaskBrowseDelivery] = Field(default_factory=list)
    active_deliveries: list[TaskBrowseDelivery] = Field(default_factory=list)
    last_run_at: datetime | None = None
    link: str | None = None
    github_meta: dict[str, str] | None = None
    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    billed_cost_usd: float = 0.0
    billed_trial_count: int = 0
    billed_has_estimated: bool = False
    billed_has_native: bool = False
    # QA/analysis spend for this task's trials, joined through ``trials``
    # because ``analysis_costs.task_id`` is NULL on trial-scoped QA rows.
    qa_cost_usd: float = 0.0
    # CUA / verifier LLM spend. Never folded into ``cost_usd`` or quotas.
    verifier_cost_usd: float = 0.0
    latest_trials: list[TaskBrowseTrial] = Field(default_factory=list)
    latest_trials_truncated: bool = False
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)
    user_tags: list[UserTagRef] = Field(default_factory=list)


class TaskBrowseResponse(BaseModel):
    items: list[TaskBrowseItem]
    limit: int
    offset: int
    has_more: bool


class TaskBrowseCountResponse(BaseModel):
    """Tasks matching a filter set across every page.

    Served by ``GET /tasks/browse?count_only=true`` on both the hosted and
    self-hosted routes -- the dashboard reaches it through its own
    ``/api/tasks/browse/count`` proxy. Kept separate from the page because the
    count is the same for every page of one filter set, so pairing the two
    would re-run the (expensive) filtered count on each pager click.
    """

    total: int


class TaskBrowseIdsResponse(BaseModel):
    """Task ids of a whole filter set, in page order.

    Served by ``GET /tasks/browse?ids_only=true``; the dashboard's "Select all
    N" reaches it through ``/api/tasks/browse/ids``. ``truncated`` is set when
    the set was cut at the server's ceiling, so a selection built from it is
    not the full match and the caller must say so.
    """

    ids: list[str]
    truncated: bool = False


class AgentModelFacet(BaseModel):
    """A distinct (agent, model) pair a trial ran. ``model`` is null for legacy
    rows with no recorded model."""

    agent: str
    model: str | None = None


class TaskBrowseFacets(BaseModel):
    """Distinct values for populating the task-browser filter controls.

    Trial-derived facets are scoped to the org's non-superseded
    trials. Enum-valued filters (task status, priority, trial status, origin)
    are static and supplied client-side, so they are not returned here.

    ``experiments`` is deprecated and always empty; it used to carry every org
    experiment (7.7MB at 126k experiments). Experiment filter options are
    served by ``GET /tasks/browse/experiment-options`` instead. The field is
    kept so the response shape does not break existing consumers.
    """

    agents: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    agent_models: list[AgentModelFacet] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    harbor_stages: list[str] = Field(default_factory=list)
    analysis_classifications: list[str] = Field(default_factory=list)
    # Customers a task can have been delivered to: every ``customers`` row
    # plus every imported history label not yet mapped to one. Values are
    # what the ``delivered_to`` / ``not_delivered_to`` browse filters accept.
    delivery_customers: list[str] = Field(default_factory=list)
    # Distinct, unretracted ``category`` assertions from the metadata import.
    categories: list[str] = Field(default_factory=list)
    # Deprecated: always empty — see the class docstring.
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)


class TaskStatusResponse(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    user: str
    github_username: str | None = None
    github_meta: dict[str, str] | None = None
    link: str | None = None
    task_path: str
    experiment_id: str
    experiment_name: str
    experiment_is_public: bool = False
    experiment_created_at: datetime | None = None
    experiment_owner: str | None = None
    experiment_link: str | None = None
    experiments: list[TaskBrowseExperiment] = Field(
        default_factory=list,
        description=(
            "All live experiments this task belongs to. The singular "
            "experiment_* fields keep the primary one for compatibility."
        ),
    )
    current_version: int | None = None
    current_version_id: str | None = None
    trial_version: int | None = None
    trial_version_id: str | None = None
    total: int
    completed: int
    failed: int
    # SKIPPED trials are terminal non-passes: included in ``total`` but not in
    # ``completed``/``failed``. Exposed so clients can compute
    # ``active = total - completed - failed - skipped`` instead of treating
    # skipped as still-running.
    skipped: int = 0
    progress: str  # e.g., "5/10 completed"
    reward_success: int | None = None
    reward_sum: float | None = None
    reward_total: int | None = None
    run_analysis: bool = False
    review_version_matches: bool | None = None
    verdict_status: VerdictStatus | None = None
    verdict: dict | None = None
    verdict_error: str | None = Field(
        None,
        description="Error message if verdict computation failed",
    )
    jobs: list[VisibleWorkerJob] = Field(
        default_factory=list,
        description="Active/recent worker_jobs rows for this task and its trials",
    )
    trials: list[TrialResponse] | None = None
    user_tags: list[UserTagRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class TaskPanelResponse(BaseModel):
    task: TaskStatusResponse
    version: TaskVersionSummary | None = None
    can_retry: bool
    cancel: Literal["task", "qa"] | None = None
    active_trials: int = 0
    qa_active: bool = False
    can_run_qa: bool = False
    has_analysis: bool = False


class PublicTaskStatusResponse(BaseModel):
    """Anonymous task payload with only fields required by public task pages."""

    id: str
    name: str
    status: TaskStatus
    priority: Priority
    github_meta: dict[str, str] | None = None
    task_path: str
    experiment_id: str
    experiment_name: str
    experiment_is_public: bool = False
    experiment_created_at: datetime | None = None
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)
    current_version: int | None = None
    current_version_id: str | None = None
    trial_version: int | None = None
    trial_version_id: str | None = None
    total: int
    completed: int
    failed: int
    skipped: int = 0
    progress: str
    reward_success: int | None = None
    reward_sum: float | None = None
    reward_total: int | None = None
    verdict_status: VerdictStatus | None = None
    verdict: dict | None = None
    verdict_error: str | None = None
    trials: list[TrialResponse] | None = None
    user_tags: list[UserTagRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class TaskOpenVersionRef(BaseModel):
    id: str
    version: int
    message: str | None = None
    created_at: datetime
    is_current: bool = False


class TaskOpenVerdict(BaseModel):
    """Compact verdict presentation and its originating QA trial."""

    graded_by: str | None = Field(default=None, alias="_graded_by")
    is_good: bool | None = None
    confidence: str | None = None
    primary_issue: str | None = None
    reasoning: str | None = None
    recommendations: list[str] = Field(default_factory=list)


class TaskOpenAgentModelSummary(BaseModel):
    """Exact selected-version rollup for one task-page agent/model card."""

    agent: str
    model: str | None = None
    reasoning_effort: str | None = None
    providers: list[str] = Field(default_factory=list)
    trial_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    reward_sum: float = 0.0
    reward_total: int = 0
    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    billed_cost_usd: float = 0.0
    billed_trial_count: int = 0
    billed_has_estimated: bool = False
    billed_has_native: bool = False
    last_run_at: datetime | None = None
    duration_sum_seconds: float = 0.0
    duration_trial_count: int = 0


class TaskOpenVersionSummary(TaskVersionRollup):
    effort: TaskEffortSummary = Field(default_factory=TaskEffortSummary)
    run_summary: TaskRunSummary | None = None
    """Selected-version fields owned by the bounded task-open resource."""

    must_fix_count: int = 0
    pre_trial_must_fix_count: int = 0

    user_tags: list[UserTagRef] = Field(default_factory=list)
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)
    agent_models: list[TaskOpenAgentModelSummary] = Field(default_factory=list)


class TaskOpenTask(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    user: str
    github_username: str | None = None
    github_meta: dict[str, str] | None = None
    link: str | None = None
    task_path: str
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)
    current_version: int | None = None
    current_version_id: str | None = None
    user_tags: list[UserTagRef] = Field(default_factory=list)
    run_analysis: bool = False
    review_version_matches: bool | None = None
    verdict_status: VerdictStatus | None = None
    verdict: TaskOpenVerdict | None = None
    verdict_error: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskOpenTotals(TaskCostTotals):
    token_count: int = 0
    token_trial_count: int = 0


class TaskOpenTrialRef(BaseModel):
    id: str
    name: str
    experiment_id: str | None = None
    task_version_id: str | None = None
    agent: str
    provider: str
    model: str | None = None
    reasoning_effort: str | None = None
    kind: str = "agent"
    status: TrialStatus
    reward: float | None = None
    error_kind: str | None = None
    cost_usd: float | None = None
    cost_is_estimated: bool | None = None
    is_billed: bool = False
    playback: TrialPlayback | None = None
    has_trajectory: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskOpenResponse(BaseModel):
    """Bounded first-paint resource for ``GET /tasks/{task_id}/open``."""

    task: TaskOpenTask
    default_version: TaskOpenVersionRef | None = None
    selected_version: TaskOpenVersionSummary | None = None
    totals: TaskOpenTotals = Field(default_factory=TaskOpenTotals)
    active_qa_trial: TaskOpenTrialRef | None = None
    trials: list[TaskOpenTrialRef] = Field(default_factory=list)
    trials_has_more: bool = False


# =============================================================================
# Trial Import (off-oddish Harbor runs -> existing task)
# =============================================================================


class ImportedTrialSpec(BaseModel):
    """Per-trial metadata for an off-oddish Harbor execution.

    The CLI extracts these fields from a ``harbor.models.trial.result.TrialResult``
    and posts them to ``/trials/import/init``. The server creates a
    ``TrialModel`` row in terminal state with ``origin=IMPORTED`` and
    returns a presigned PUT URL for the artifact tarball; the client
    then PUTs the archive and calls ``/trials/import/complete``.
    """

    agent: str = Field(..., description="Agent name (e.g., 'claude-code')")
    model: str | None = Field(
        None, description="Model name (normalized server-side via settings)"
    )
    environment: EnvironmentType | None = Field(
        None, description="Execution backend that actually ran the trial"
    )
    status: TrialStatus = Field(
        TrialStatus.SUCCESS,
        description=(
            "Terminal status for the imported trial. Must be SUCCESS or "
            "FAILED -- imports never enter the queue."
        ),
    )
    reward: float | None = Field(
        None, description="Verifier score in [0, 1]; None if no verifier result"
    )
    result: dict | None = Field(
        None,
        description=(
            "Structured verifier metrics and normalized report summary extracted "
            "from the imported artifacts"
        ),
    )
    error_message: str | None = Field(
        None, description="Execution error message, if any"
    )
    harbor_stage: str | None = Field(
        "completed",
        description="Harbor lifecycle stage (defaults to 'completed' for imports)",
    )
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    trajectory_duration_seconds: float | None = None
    total_tool_calls: int | None = None
    tool_counts: dict[str, int] | None = None
    cost_usd: float | None = None
    phase_timing: dict | None = Field(
        None,
        description=(
            "Per-phase duration breakdown matching the live schema: "
            "{environment_setup, agent_setup, agent_execution, verifier}"
        ),
    )
    has_trajectory: bool = Field(
        False,
        description=(
            "Whether the uploaded archive contains a readable ATIF trajectory "
            "JSON object"
        ),
    )
    harbor_config: dict | None = Field(
        None,
        description="Serialized Harbor config used during external execution",
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    external_trial_id: str | None = Field(
        None,
        description=(
            "Harbor TrialResult UUID (or any stable external ID). Combined with "
            "the target experiment to detect duplicates; keys longer than 64 "
            "characters are hashed. Re-importing into the same experiment "
            "returns 409 with the existing trial ID."
        ),
    )
    imported_at: datetime | None = Field(
        None,
        description=(
            "Bulk-migration marker (Sauron->Oddish). When set it is written on "
            "the trial row IN the import transaction, so the QA pipeline's "
            "imported_at-based exclusion can never race a follow-up UPDATE. "
            "Leave None for ad-hoc imports (stock analysis behavior)."
        ),
    )

    @model_validator(mode="after")
    def _validate_terminal_status(self) -> "ImportedTrialSpec":
        if self.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
            raise ValueError("Imported trials must have status SUCCESS or FAILED")
        return self

    @model_validator(mode="after")
    def _normalize_model(self) -> "ImportedTrialSpec":
        self.model = normalize_model_id(self.model)
        return self


class TrialImportInitRequest(BaseModel):
    """Request to create an imported trial row + presigned artifact URL."""

    task_id: str = Field(
        ..., description="Existing task ID (upload via `oddish upload` first)"
    )
    experiment_id: str | None = Field(
        None,
        description=(
            "Experiment ID or name to attach the trial to. Creates the "
            "experiment if the name does not exist. When None, a fresh "
            "auto-named experiment is created (matching `oddish run`'s "
            "default behaviour)."
        ),
    )
    trial: ImportedTrialSpec = Field(..., description="Imported trial metadata")
    upload_artifacts: bool = Field(
        True,
        description=(
            "When True, the response includes a presigned PUT URL for a "
            "``.oddish-trial-import.tar.gz`` staging archive that the "
            "client then uploads and finalizes with /trials/import/complete. "
            "When False, the trial row is created without any artifacts "
            "and complete does not need to be called."
        ),
    )


class TrialImportInitResponse(BaseModel):
    """Response for `/trials/import/init`."""

    trial_id: str
    task_id: str
    experiment_id: str
    experiment_name: str
    trial_s3_key: str | None = Field(
        None,
        description="S3 prefix where the trial artifacts will live once uploaded",
    )
    archive_s3_key: str | None = Field(
        None,
        description="S3 key the client should PUT the archive tarball to",
    )
    upload_url: str | None = Field(
        None, description="Presigned PUT URL for the archive"
    )
    upload_method: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    requires_completion: bool = Field(
        False,
        description="Whether the client must call /trials/import/complete after PUT",
    )


class TrialImportCompleteRequest(BaseModel):
    """Finalize an imported trial after the artifact archive was uploaded."""

    trial_id: str


class TrialImportCompleteResponse(BaseModel):
    """Response for `/trials/import/complete`."""

    trial_id: str
    trial_s3_key: str
    files_extracted: int


# =============================================================================
# Public Sharing Models
# =============================================================================


class PublicExperimentResponse(BaseModel):
    """Public experiment metadata."""

    name: str
    public_token: str
    description: str | None = None
    show_qa: bool = False
    show_costs: bool = True


class PublicTaskQaResponse(BaseModel):
    """Source checks for one task version visible through a share link."""

    version: int | None = None
    version_id: str | None = None
    pre_trial_findings: list[dict] = Field(default_factory=list)
    pre_trial_status: str | None = None
    pre_trial_error: str | None = None


class PublicExperimentListItem(BaseModel):
    """Public dataset list item."""

    id: str
    name: str
    public_token: str
    task_count: int
    created_at: str


class TagCreateRequest(BaseModel):
    key: str
    value: str | None = None
    color: str | None = None
    description: str | None = None
    visibility: str = "PRIVATE"


class TagUpdateRequest(BaseModel):
    key: str | None = None
    color: str | None = None
    description: str | None = None
    expected_row_version: int


class TagSetVisibilityRequest(BaseModel):
    visibility: str
    expected_row_version: int


class TagArchiveRequest(BaseModel):
    expected_row_version: int


class TagMergeRequest(BaseModel):
    target_tag_id: str
    expected_row_version: int | None = None


class TagListItem(BaseModel):
    id: str
    key: str
    value: str | None = None
    color: str | None = None
    visibility: str
    state: str
    usage_count: int = 0
    row_version: int = 1
    owner_user_id: str | None = None
    # Per-scope assignment breakdown (active assignments only). Summed across
    # all scopes these equal ``usage_count``. Populated by the tags list
    # endpoint; other endpoints that build a TagListItem leave them at 0.
    task_count: int = 0
    version_count: int = 0
    experiment_count: int = 0
    # Resolved display label / avatar for ``owner_user_id`` (creator). Populated
    # by the tags list endpoint via a join against the ``users`` table.
    owner_label: str | None = None
    owner_avatar_url: str | None = None


class TagListResponse(BaseModel):
    items: list[TagListItem]


class TagAssignRequest(BaseModel):
    tag_id: str
    scope: str
    target_id: str
    mode: str | None = None  # 'snapshot' | 'living' for EXPERIMENT
    task_id: str | None = None

    @model_validator(mode="after")
    def _validate_scope(self):
        if self.scope not in {"VERSION", "TASK", "EXPERIMENT"}:
            raise ValueError(
                f"scope must be VERSION/TASK/EXPERIMENT (got {self.scope})"
            )
        if self.scope == "EXPERIMENT" and self.mode not in {"snapshot", "living"}:
            raise ValueError("EXPERIMENT-scope apply requires mode='snapshot'|'living'")
        return self


class TagUnassignRequest(BaseModel):
    tag_id: str
    scope: str
    target_id: str
    task_id: str | None = None


class TagAssignResponse(BaseModel):
    tag_id: str
    assignment_id: str | None = None
    mode: str | None = None
    materialized: int | None = None


class TagExcludeRequest(BaseModel):
    tag_id: str
    experiment_id: str
    scope: str
    target_id: str
    task_id: str | None = None


class TagGrantCreateRequest(BaseModel):
    principal_type: str
    principal_user_id: str | None = None
    capability: str


class TagGrantListItem(BaseModel):
    id: str
    principal_type: str
    principal_user_id: str | None = None
    capability: str


class TagGrantListResponse(BaseModel):
    items: list[TagGrantListItem]


class TagPolicyResponse(BaseModel):
    org_id: str
    max_tags_per_entity: int
    name_max_len: int
    name_charset: str
    reserved_prefixes: list[str]
    who_can_create: str
    profanity_mode: str
    profanity_allowlist: list[str]
    profanity_denylist: list[str]


class TagPolicyUpdateRequest(BaseModel):
    max_tags_per_entity: int | None = None
    name_max_len: int | None = None
    name_charset: str | None = None
    reserved_prefixes: list[str] | None = None
    who_can_create: str | None = None
    profanity_mode: str | None = None
    profanity_allowlist: list[str] | None = None
    profanity_denylist: list[str] | None = None


class TagFilterASTDTO(BaseModel):
    all: list[str] = Field(default_factory=list)
    any_: list[str] = Field(default_factory=list)
    none: list[str] = Field(default_factory=list)


class SavedTagFilterCreateRequest(BaseModel):
    name: str
    filter_ast: dict
    visibility: str = "PRIVATE"


class SavedTagFilterUpdateRequest(BaseModel):
    name: str | None = None
    filter_ast: dict | None = None
    visibility: str | None = None


class SavedTagFilterItem(BaseModel):
    id: str
    name: str
    filter_ast: dict
    visibility: str
    owner_user_id: str


class SavedTagFilterListResponse(BaseModel):
    items: list[SavedTagFilterItem]


class ProfanityReportItem(BaseModel):
    event_id: int
    tag_id: str | None
    org_id: str | None
    actor_user_id: str | None
    reason: str | None
    payload: dict
    occurred_at: datetime


class ProfanityReportListResponse(BaseModel):
    items: list[ProfanityReportItem]


class ProfanityReportCreateRequest(BaseModel):
    tag_id: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Experiment typeahead — shared item shape for the task browser.
# ---------------------------------------------------------------------------
class ExperimentOption(BaseModel):
    id: str
    name: str


class ExperimentOptionsResponse(BaseModel):
    """Typeahead options for the task-browser experiment filter.

    Served by ``GET /tasks/browse/experiment-options``. Replaces the retired
    ``TaskBrowseFacets.experiments`` all-org list with a bounded, searchable
    page, reusing the adjacent ``ExperimentOption`` item shape.
    """

    items: list[ExperimentOption] = Field(default_factory=list)


FEEDBACK_BODY_MAX_CHARS = 20_000
FeedbackTarget = Literal[
    "qa_verdict", "qa_action_item", "qa_trajectory", "qa_task_verdict", "qa_pre_trial"
]
FeedbackVote = Literal["agree", "disagree"]


class FeedbackCreate(BaseModel):
    """Agree or disagree with one analysis report or action item."""

    body: str = Field(default="", max_length=FEEDBACK_BODY_MAX_CHARS)
    target: FeedbackTarget
    target_key: str = Field(min_length=1, max_length=160)
    vote: FeedbackVote
    trial_id: str = Field(min_length=1)
    report_trial_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def normalize_text(self) -> "FeedbackCreate":
        self.body = self.body.strip()
        self.target_key = self.target_key.strip()
        self.trial_id = self.trial_id.strip()
        if not self.target_key or not self.trial_id:
            raise ValueError("target_key and trial_id cannot be blank")
        if self.report_trial_id is not None:
            self.report_trial_id = self.report_trial_id.strip()
            if not self.report_trial_id:
                raise ValueError("report_trial_id cannot be blank")
        if self.target in {"qa_trajectory", "qa_task_verdict", "qa_pre_trial"}:
            if not self.report_trial_id:
                raise ValueError("report_trial_id is required for report feedback")
        return self


class FeedbackResponse(BaseModel):
    """One persisted QA vote, as returned by the create and list endpoints."""

    id: str
    created_by_user_id: str | None = None
    experiment_id: str
    trial_id: str
    report_trial_id: str | None = None
    target: FeedbackTarget
    target_key: str
    vote: FeedbackVote
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


# =============================================================================
# Deliveries (docs/delivery-design.md)
# =============================================================================

# "waived": the check fails, but a person acknowledged shipping it anyway.
DeliveryCheckStatus = Literal["pass", "fail", "off", "waived"]
DeliveryCheckKind = Literal["automated", "manual"]
ManualCheckScope = Literal["task", "delivery"]


class ManualCheckDefinition(BaseModel):
    """One human-ticked check defined in a delivery's check_config."""

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=255)
    scope: ManualCheckScope = "task"


class DeliveryCheckConfig(BaseModel):
    """Stored shape of ``deliveries.check_config``.

    ``automated`` holds per-check overrides merged over the defaults in
    ``oddish.core.deliveries.DEFAULT_AUTOMATED_CHECKS``; unknown keys are
    rejected so a typo cannot silently disable a check.
    """

    automated: dict[str, dict] = Field(default_factory=dict)
    manual: list[ManualCheckDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_manual_keys(self) -> "DeliveryCheckConfig":
        keys = [m.key for m in self.manual]
        if len(keys) != len(set(keys)):
            raise ValueError("manual check keys must be unique")
        return self


class DeliveryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Every delivery ships to a customer: an existing customer's id or
    # name, or a new name (the server creates the customer row).
    customer: str = Field(min_length=1, max_length=255)
    description: str | None = None
    check_config: DeliveryCheckConfig | None = None
    task_ids: list[str] = Field(default_factory=list, max_length=5000)


class DeliveryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    customer: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    check_config: DeliveryCheckConfig | None = None


class DeliveryTasksAdd(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=5000)


class ManualCheckSet(BaseModel):
    """Tick or untick one manual check."""

    check_key: str = Field(min_length=1, max_length=64)
    # Required for task-scoped checks; must be omitted for delivery-scoped.
    delivery_task_id: str | None = None
    # Required for positive sign-off/exception decisions; otherwise optional.
    # When supplied (including null), reject changes to the displayed version.
    expected_version_id: str | None = None
    checked: bool
    note: str = Field(default="", max_length=4000)


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class CustomerResponse(BaseModel):
    id: str
    name: str

    model_config = {"from_attributes": True}


class DeliveryResponse(BaseModel):
    id: str
    name: str
    customer_id: str | None = None
    # Resolved from the customer row; None on legacy rows only.
    customer_name: str | None
    description: str | None
    status: str
    is_public: bool
    finalized_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DeliveryListItem(DeliveryResponse):
    task_count: int


class DeliveryCheckResult(BaseModel):
    # Short unmet requirements; absent in older finalized snapshots.
    failure_labels: list[str] = Field(default_factory=list)
    key: str
    kind: DeliveryCheckKind
    label: str
    status: DeliveryCheckStatus
    detail: str = ""
    # Manual checks only: who ticked it and when. The hosted API fills
    # ``checked_by_name`` from the user record at read time; the core
    # leaves it None.
    checked_by_user_id: str | None = None
    checked_by_name: str | None = None
    checked_at: datetime | None = None


class DeliveryDefect(BaseModel):
    """One reported defect; acknowledgment permits a version-specific exception."""

    id: str
    title: str
    source: str  # "pre_trial" | "trial"
    finding_id: str | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    # Defaults keep committed pre-policy snapshots readable.
    recorded_tier: str | None = None
    finding: dict | None = None
    reporting_trial_id: str | None = None
    review_trial_id: str | None = None
    acknowledged: bool
    acknowledged_by_user_id: str | None = None
    acknowledged_by_name: str | None = None
    acknowledged_at: datetime | None = None


QAIssueCategory = Literal[
    "instructions", "verifier", "environment", "evidence", "qa_execution"
]


class QAWorkMetadata(BaseModel):
    owner_user_id: str | None = None
    claimed_at: datetime | None = None
    issue_categories: list[QAIssueCategory] = Field(default_factory=list, max_length=5)
    note: str = Field(default="", max_length=4000)


class QAWorkPatch(BaseModel):
    version_id: str
    release: bool = False
    issue_categories: list[QAIssueCategory] | None = Field(default=None, max_length=5)
    note: str | None = Field(default=None, max_length=4000)


class QAWorkClaim(BaseModel):
    version_ids: list[str] = Field(min_length=1, max_length=5000)
    limit: int = Field(default=25, ge=1, le=100)


class QAWorkAssign(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=1000)
    assignee: str = Field(min_length=1, max_length=320)
    replace: bool = False


class QAWorkAssignResponse(BaseModel):
    owner_user_id: str
    assigned_task_ids: list[str] = Field(default_factory=list)
    unchanged_task_ids: list[str] = Field(default_factory=list)
    skipped_task_ids: list[str] = Field(default_factory=list)


class DeliveryQAStatus(BaseModel):
    status: Literal[
        "never", "queued", "running", "error", "outdated", "accepted", "needs_fixes"
    ] = "never"
    trial_id: str | None = None
    finished_at: datetime | None = None
    detail: str = "No QA verdict recorded"


class DeliveryTaskBoardRow(BaseModel):
    delivery_task_id: str
    task_id: str
    task_name: str
    version_id: str | None
    version: int | None
    pinned_version_id: str | None
    newer_version_exists: bool
    is_visible: bool
    sort_order: int
    customer_note: str | None
    internal_note: str | None
    checks: list[DeliveryCheckResult]
    defects: list[DeliveryDefect] = Field(default_factory=list)
    qa: DeliveryQAStatus = Field(default_factory=DeliveryQAStatus)
    pre_trial_status: str | None = "unknown"
    pre_trial_error: str | None = None
    verdict_status: str | None = None
    verdict_error: str | None = None
    qa_work: QAWorkMetadata = Field(default_factory=QAWorkMetadata)
    qa_owner_name: str | None = None
    ready: bool


class DeliveryOwnerProgress(BaseModel):
    task_count: int = 0
    ready: int = 0


class DeliveryProgressPoint(BaseModel):
    recorded_at: datetime
    task_count: int
    ready: int
    blocked: int
    awaiting_signoff: int
    unassigned: int
    open_findings: int
    acknowledged_findings: int
    # None means the observation predates owner history; an empty map means zero tasks.
    owners: dict[str, DeliveryOwnerProgress] | None = None


class DeliveryBoardResponse(BaseModel):
    progress_history: list[DeliveryProgressPoint] = Field(default_factory=list)
    qa_as_of: datetime | None = None
    qa_viewer_user_id: str | None = None
    delivery: DeliveryResponse
    check_config: DeliveryCheckConfig
    tasks: list[DeliveryTaskBoardRow]
    delivery_checks: list[DeliveryCheckResult]
    ready: bool
    ready_task_count: int
    task_count: int
    # Set when status == finalized: the board above is the stored snapshot,
    # not a live computation.
    frozen: bool = False
    finalized_at: datetime | None = None


DeliveryTaskState = Literal["needs_work", "qa_incomplete", "awaiting_signoff", "ready"]


class DeliveryViewQuery(BaseModel):
    page: int = Field(default=1, ge=1, le=9007199254740991)
    per_page: int = Field(default=25, json_schema_extra={"enum": [10, 25, 50, 100]})
    filter: Literal[
        "all",
        "outstanding",
        "blocked",
        "needs_work",
        "qa_incomplete",
        "awaiting_signoff",
        "ready",
    ] = "all"
    issue: Literal[
        "all", "instructions", "verifier", "environment", "evidence", "qa_execution"
    ] = "all"
    owner: str = Field(default="all", max_length=512)
    group: Literal["none", "owner", "state", "issue"] = "none"
    task: str | None = Field(default=None, max_length=512)

    @field_validator("per_page")
    @classmethod
    def valid_page_size(cls, value: int) -> int:
        if value not in (10, 25, 50, 100):
            raise ValueError("per_page must be 10, 25, 50 or 100")
        return value


class DeliverySelectionItem(BaseModel):
    delivery_task_id: str
    task_id: str
    task_name: str
    version_id: str | None
    version: int | None
    state: DeliveryTaskState
    can_sign_off: bool
    qa_status: str


class DeliveryPageRow(DeliveryTaskBoardRow):
    state: DeliveryTaskState


class DeliveryPageResponse(DeliveryBoardResponse):
    """Only one page carries findings; IDs retain inventory and bulk-selection scope."""

    tasks: list[DeliveryPageRow]
    page: int
    per_page: int
    total: int
    focus_task_id: str | None
    focus_outside_filters: bool
    owner_counts: dict[DeliveryTaskState, int]
    owners: dict[str, str]
    member_task_ids: list[str]
    matching_task_ids: list[str]


class TaskQAHistoryRun(BaseModel):
    trial_id: str
    kind: str
    status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    # Why a failed run failed: the trial's execution error, else its
    # analysis error.
    error: str | None = None


class TaskQAHistoryFinding(BaseModel):
    tier: str
    title: str
    source: str  # "pre_trial" | "trial"


class TaskQAHistoryDecision(BaseModel):
    id: str
    delivery_id: str
    check_key: str
    checked_by_user_id: str | None
    checked_at: datetime
    note: str

    model_config = {"from_attributes": True}


class TaskQAHistoryVersion(BaseModel):
    version_id: str
    version: int
    created_at: datetime
    message: str | None
    is_current: bool
    pre_trial_status: str | None
    pre_trial_finished_at: datetime | None
    pre_trial_error: str | None = None
    # Reported defects from every source, including historical tiers;
    # kept under the existing API name for compatibility.
    must_fix: int
    rollout_count: int
    rollout_agents: int
    qa_runs: list[TaskQAHistoryRun]
    # The QA findings behind the counts, for inline display: every
    # recorded defect, with its original severity.
    findings: list[TaskQAHistoryFinding] = Field(default_factory=list)
    decisions: list[TaskQAHistoryDecision] = Field(default_factory=list)


class TaskInventoryCategory(BaseModel):
    source: str
    value: str


class TaskInventoryTask(BaseModel):
    id: str
    name: str
    org_id: str
    task_path: str
    retired_at: str | None = None
    current_version_id: str | None = None
    current_content_hash: str | None = None
    categories: list[str] = Field(default_factory=list)
    category_evidence: list[TaskInventoryCategory] = Field(default_factory=list)


class TaskInventoryResponse(BaseModel):
    """Current task identities of one organization, retired tasks included.

    Input to the delivery metadata planner (``docs/delivery-metadata-backfill.md``).
    """

    schema_version: str
    org_id: str
    captured_at: str
    tasks: list[TaskInventoryTask]


class HistoryImportReceipt(BaseModel):
    """One preview, apply, or rejected run of a delivery metadata plan."""

    id: str
    org_id: str
    plan_schema: str
    plan_hash: str
    mode: str
    outcome: str
    rejection_reason: str | None = None
    source_as_of: str | None = None
    source_revision: str | None = None
    inventory_captured_at: datetime | None = None
    input_hashes: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)
    created_by_user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskQAHistoryResponse(BaseModel):
    task_id: str
    task_name: str
    current_version_id: str | None
    verdict: dict | None
    verdict_status: str | None
    # The version the stored verdict covers: the one graded by the newest
    # verdict-producing QA run. None when no such run exists.
    verdict_version_id: str | None = None
    versions: list[TaskQAHistoryVersion]
    # QA/audit trials with no version id (legacy rows): shown apart so the
    # record stays complete without guessing which version they graded.
    unversioned_runs: list[TaskQAHistoryRun] = Field(default_factory=list)


# =============================================================================
# QA issue reports (oddish.core.qa_issue_report)
# =============================================================================


class QAIssueReportResponse(BaseModel):
    """A live QA issue report. ``tasks`` is filled only for one ``category``."""

    scope_kind: Literal["experiment", "delivery"]
    scope_id: str
    scope_name: str
    taxonomy_version: str
    summary: dict[str, int]
    # Blocked / clear / waiting over time, replayed from task versions.
    burndown: list[dict[str, Any]] = Field(default_factory=list)
    # Set when the report shows each task at its k-th version.
    version: int | None = None
    categories: list[dict[str, Any]]
    category: str | None = None
    task_total: int = 0
    tasks: list[dict[str, Any]] = Field(default_factory=list)
