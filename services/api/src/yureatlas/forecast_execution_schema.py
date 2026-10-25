"""Versioned durable forecast targets and complete publication resolutions."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import digest
from .timeutil import utc


class ForecastTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    state: Literal["target_pinned"]
    start: AwareDatetime
    end: AwareDatetime
    input_cutoff: AwareDatetime
    mode: Literal["prospective"]
    target_basis: Literal[
        "explicit_start",
        "request_time",
        "legacy_explicit_start",
        "legacy_request_time",
        "inline_request_time",
    ]
    request_time: AwareDatetime

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Forecast execution version must be an integer")
        return value

    @field_validator("start", "end", "input_cutoff", "request_time", mode="before")
    @classmethod
    def aware_instants(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("Forecast execution times require aware timestamps")  # noqa: TRY004 - Pydantic
        return utc(value)

    @model_validator(mode="after")
    def exact_window(self):
        if self.end - self.start != timedelta(days=1):
            raise ValueError("Forecast execution requires an exact 24-hour target window")
        if self.start - self.input_cutoff != timedelta(minutes=30):
            raise ValueError("Forecast input cutoff must be exactly 30 minutes before target start")
        if (
            self.target_basis in {"request_time", "legacy_request_time", "inline_request_time"}
            and self.input_cutoff != self.request_time
        ):
            raise ValueError("An implicit forecast target must be 30 minutes after its request")
        return self


class ForecastSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["forecast-selection-v1"]
    kind: Literal["requested", "promotion", "historical_rate"]
    selected_at: AwareDatetime
    promotion_id: str | None
    promotion_created_at: AwareDatetime | None
    promotion_reason: str | None
    model_artifact: str | None
    model_training_cutoff: AwareDatetime
    model_available_at: AwareDatetime

    @field_validator(
        "selected_at", "promotion_created_at", "model_training_cutoff", "model_available_at", mode="before"
    )
    @classmethod
    def aware_instants(cls, value):
        if value is None:
            return value
        if not isinstance(value, (str, datetime)):
            raise ValueError("Forecast selection times require aware timestamps")  # noqa: TRY004 - Pydantic
        return utc(value)

    @model_validator(mode="after")
    def promotion_evidence(self):
        fields = (self.promotion_id, self.promotion_created_at, self.promotion_reason)
        if self.kind == "promotion":
            if any(value is None for value in fields) or not self.promotion_id:
                raise ValueError("Promoted model selection requires its promotion evidence")
        elif any(value is not None for value in fields):
            raise ValueError("Only promoted model selection may contain promotion evidence")
        return self


class ResolvedForecastExecution(ForecastTarget):
    state: Literal["resolved"]
    experiment_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    forecast_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: ForecastSelection

    @model_validator(mode="after")
    def canonical_identity(self):
        expected = digest(
            {"experiment": self.experiment_id, "model": self.model_id, "start": self.start, "mode": self.mode}
        )
        if self.forecast_key != expected:
            raise ValueError("Resolved forecast identity does not match its target and model")
        return self


ForecastExecution = Annotated[ForecastTarget | ResolvedForecastExecution, Field(discriminator="state")]
