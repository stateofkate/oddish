"""Dependency-light, bounded contracts for retained environment setup evidence."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SetupEventKind(StrEnum):
    ENV_SUBPHASE = "env_subphase"
    ENV_INFO = "env_info"


class SetupStepStatus(StrEnum):
    """Mirrors Harbor's ``SubphaseStatus`` values."""

    STARTED = "started"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class SetupCategory(StrEnum):
    SETUP = "setup"
    RESOLVE = "resolve"
    PROVISION = "provision"
    STAGE = "stage"
    BUILD = "build"
    START = "start"
    READY = "ready"
    FINALIZE = "finalize"
    CLEANUP = "cleanup"
    OTHER = "other"


class SetupOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1, 2] = 1
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    parent_operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    category: SetupCategory = SetupCategory.OTHER
    activity_kind: Literal["operation", "supporting"] = "operation"
    resource_layer: Literal["task_environment"] = "task_environment"
    strategy: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=128)
    operation_attempt: int = Field(default=1, ge=1)
    finished_at: datetime | None = None
    phase: str = Field(max_length=128)
    status: SetupStepStatus
    started_at: datetime
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_type: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_operation_identity(self) -> "SetupOperation":
        if self.schema_version == 2 and not self.operation_id:
            raise ValueError("v2 setup events require operation_id")
        if self.operation_id and self.parent_operation_id == self.operation_id:
            raise ValueError("An operation cannot be its own parent")
        return self


class CompletedEnvironmentActivity(SetupOperation):
    """One flat terminal measurement, piggybacked on an existing progress event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    operation_id: str = Field(min_length=1, max_length=128)
    status: Literal["success", "error", "cancelled"]
    finished_at: datetime
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)


class EnvSubphasePayload(SetupOperation):
    kind: Literal[SetupEventKind.ENV_SUBPHASE] = SetupEventKind.ENV_SUBPHASE
    # None means a legacy producer; [] means the activity-aware producer had
    # no completed measurements to attach to this notification.
    completed_activities: list[CompletedEnvironmentActivity] | None = Field(
        default=None, max_length=128
    )


class EnvInfoPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal[SetupEventKind.ENV_INFO] = SetupEventKind.ENV_INFO
    provider: str = Field(max_length=128)
    external_id: str | None = Field(default=None, max_length=256)
    instance_type: str | None = Field(default=None, max_length=256)
    region: str | None = Field(default=None, max_length=256)


SetupPayload = Annotated[
    EnvSubphasePayload | EnvInfoPayload, Field(discriminator="kind")
]


class ProgressEvent(BaseModel):
    attempt: int = Field(gt=0)
    event_key: str = Field(max_length=96)
    kind: SetupEventKind
    source_seq: int = Field(ge=0)
    schema_version: Literal[1, 2] = 1
    recorded_at: datetime
    payload: SetupPayload


class ProgressHistory(BaseModel):
    trial_id: str
    current_attempt: int
    history_available: bool
    history_truncated: bool = False
    events: list[ProgressEvent]
