"""Requested schedules, actual worker application and bounded operator history."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ScheduleKind = Literal["ingest", "forecast", "evaluate", "reconcile", "train"]
SCHEDULE_KINDS = ("ingest", "forecast", "evaluate", "reconcile", "train")
ExperimentID = Annotated[str, Field(min_length=1, max_length=128, strict=True)]


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    scope: Literal["all_real", "selected"] = "all_real"
    experiment_ids: list[ExperimentID] = Field(default_factory=list, max_length=100)
    kinds: list[ScheduleKind] = Field(default_factory=lambda: list(SCHEDULE_KINDS), max_length=5)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.experiment_ids)) != len(self.experiment_ids) or len(set(self.kinds)) != len(
            self.kinds
        ):
            raise ValueError("Schedule experiment IDs and kinds must be unique")
        if any(value != value.strip() for value in self.experiment_ids):
            raise ValueError("Experiment IDs must not contain surrounding whitespace")
        if self.scope == "all_real" and self.experiment_ids:
            raise ValueError("All-real scope must not specify selected experiment IDs")
        self.experiment_ids = sorted(self.experiment_ids)
        self.kinds = [kind for kind in SCHEDULE_KINDS if kind in self.kinds]
        return self


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    spec: ScheduleSpec
    reason: str = Field(min_length=1, max_length=500, strict=True)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class ScheduleRevision(BaseModel):
    id: str | None
    revision: int
    created_at: AwareDatetime | None
    spec: ScheduleSpec
    reason: str


class ScheduleHistory(BaseModel):
    items: list[ScheduleRevision]
    has_more: bool
    next_before_revision: int | None


class ScheduleApplication(BaseModel):
    revision: int
    observed_at: AwareDatetime
    owner_token: str | None
    effective_enabled: bool
    status: Literal["applied", "disabled"]
    details: dict


class ScheduleWorker(BaseModel):
    status: Literal["missing", "unregistered", "offline", "stale", "clock_skew", "fresh"]
    heartbeat: AwareDatetime | None
    owner_token: str | None
    environment_enabled: bool | None
    reason: str


class NextScheduleRun(BaseModel):
    kind: ScheduleKind
    at_utc: AwareDatetime
    at_jst: str
    forecast_target_start: AwareDatetime | None = None


class ScheduleRecovery(BaseModel):
    next_check_estimate: AwareDatetime | None = Field(
        description="Estimated next reconciliation within one 30-second cadence; not an issuance promise"
    )
    eligible_forecast_target: AwareDatetime | None
    policy: str


class ScheduleStatus(BaseModel):
    checked_at: AwareDatetime
    timezone: Literal["Asia/Tokyo"] = "Asia/Tokyo"
    requested: ScheduleRevision
    api_environment_enabled: bool
    worker: ScheduleWorker
    effective_enabled: bool | None
    effective_reason: str
    application_status: Literal["current", "pending", "worker_unverified", "environment_disabled"]
    last_application: ScheduleApplication | None
    scope_count: int
    scope_complete: bool
    applicable_kinds: list[ScheduleKind]
    next_regular_runs: list[NextScheduleRun]
    recovery: ScheduleRecovery
