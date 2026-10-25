"""Typed promotion audit and current forecast-selection responses."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128, strict=True)
    reason: str = Field(min_length=5, max_length=500, strict=True)

    @field_validator("model_id", "reason", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class PromotionModel(BaseModel):
    id: str
    experiment_id: str
    algorithm: str
    status: str
    created_at: AwareDatetime
    training_cutoff: AwareDatetime
    available_at: AwareDatetime
    artifact: str | None


class PromotionRecord(BaseModel):
    id: str
    experiment_id: str
    model_id: str
    created_at: AwareDatetime
    reason: str


class PromotionHistoryItem(PromotionRecord):
    model: PromotionModel


class PromotionHistory(BaseModel):
    experiment_id: str
    limit: int = Field(ge=1, le=100)
    items: list[PromotionHistoryItem]
    has_more: bool
    next_cursor: str | None


class PromotionCreated(PromotionRecord):
    affects: Literal["future forecasts only"] = "future forecasts only"


class CurrentModelSelection(BaseModel):
    experiment_id: str
    checked_at: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    input_cutoff: AwareDatetime
    policy: str
    kind: Literal["promotion", "historical_rate", "unavailable"]
    reason_code: Literal["latest_promotion", "historical_rate_fallback", "selection_unavailable"]
    reason: str
    model: PromotionModel | None
    promotion: PromotionRecord | None
