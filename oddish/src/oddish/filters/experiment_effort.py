"""Ranges and ordering over prepared experiment measurements, before pagination."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func


class TokenMetric(StrEnum):
    TOTAL = "total"
    UNCACHED_INPUT = "uncached_input"
    CACHED_INPUT = "cached_input"
    OUTPUT = "output"

    @property
    def field(self) -> str:
        return "tokens" if self == self.TOTAL else f"{self.value}_tokens"


class ExperimentEffortFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_steps: float | None = Field(None, ge=0, allow_inf_nan=False)
    max_steps: float | None = Field(None, ge=0, allow_inf_nan=False)
    min_tokens: float | None = Field(None, ge=0, allow_inf_nan=False)
    max_tokens: float | None = Field(None, ge=0, allow_inf_nan=False)
    min_cost: float | None = Field(None, ge=0, allow_inf_nan=False)
    max_cost: float | None = Field(None, ge=0, allow_inf_nan=False)
    token_metric: TokenMetric = TokenMetric.TOTAL
    sort: Literal["recent", "avg_steps_asc", "avg_steps_desc", "avg_tokens_asc", "avg_tokens_desc", "avg_cost_asc", "avg_cost_desc"] = "recent"

    @model_validator(mode="after")
    def ordered_ranges(self):
        for field in ("steps", "tokens", "cost"):
            low, high = getattr(self, f"min_{field}"), getattr(self, f"max_{field}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"minimum average {field} exceeds maximum")
        return self

    def apply(self, query, payload):
        fields = {"steps": "steps", "tokens": self.token_metric.field, "cost": "cost"}
        averages = {
            key: payload["effort"][f"{field}_sum"].as_float()
            / func.nullif(payload["effort"][f"{field}_trial_count"].as_integer(), 0)
            for key, field in fields.items()
        }
        for field, average in averages.items():
            low, high = getattr(self, f"min_{field}"), getattr(self, f"max_{field}")
            if low is not None:
                query = query.where(average >= low)
            if high is not None:
                query = query.where(average <= high)
        if self.sort != "recent":
            field, direction = self.sort.removeprefix("avg_").rsplit("_", 1)
            value = averages[field]
            query = query.order_by((value.asc() if direction == "asc" else value.desc()).nulls_last())
        return query
