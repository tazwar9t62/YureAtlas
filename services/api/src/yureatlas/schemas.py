from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9_-]+$")
    provider: Literal["usgs", "jma", "synthetic"] = "usgs"
    target_magnitude: float = Field(default=4, ge=0, le=9)
    input_magnitude: float = Field(default=3, ge=0, le=9)
    west: float = Field(default=122, ge=115, le=160)
    east: float = Field(default=156, ge=115, le=160)
    south: float = Field(default=20, ge=15, le=55)
    north: float = Field(default=48, ge=15, le=55)
    size_m: int = Field(default=50000, ge=50000, le=200000)
    yellow: float = Field(default=.01, gt=0, lt=1)
    red: float = Field(default=.05, gt=0, lt=1)
    duration_hours: Literal[24] = 24
    magnitude_policy: str = "provider_preferred"
    cutoff_minutes_before_start: Literal[30] = 30
    missing_depth_policy: Literal["retain_null"] = "retain_null"
    evaluation_policy: Literal["complete_retrieval_provisional"] = "complete_retrieval_provisional"

    @model_validator(mode="after")
    def coherent(self):
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("Invalid study envelope")
        if self.input_magnitude > self.target_magnitude or self.yellow >= self.red:
            raise ValueError("Thresholds must be ordered")
        self.magnitude_policy = {"usgs": "USGS preferred (mixed types)", "jma": "JMA J/D/d/V/v selected",
                                 "synthetic": "artificial magnitude"}[self.provider]
        return self


class ExperimentView(BaseModel):
    id: str
    name: str
    synthetic: bool
    source_id: str
    grid_id: str
    config_hash: str
    config: dict


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str | None = None
    model_id: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    key: str = Field(default="", max_length=120)
    fit_days: int = Field(default=180, ge=30, le=730)
    tune_days: int = Field(default=30, ge=7, le=120)
    calibration_days: int = Field(default=30, ge=7, le=120)
    test_days: int = Field(default=7, ge=1, le=180)
    folds: int = Field(default=1, ge=1, le=6)

    @model_validator(mode="after")
    def bounded(self):
        if self.start and self.end and not 0 < (self.end-self.start).total_seconds() <= 366*86400*30:
            raise ValueError("Expected an ordered interval of at most 30 years")
        return self


class JobView(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    attempts: int
    error: str | None
    result: dict
    created_at: datetime
    cancel_requested: bool


class ForecastView(BaseModel):
    id: str
    experiment_id: str
    model_id: str
    issued_at: datetime
    start: datetime
    end: datetime
    input_cutoff: datetime
    mode: str
    status: str
    provenance: dict


class ModelView(BaseModel):
    id: str
    experiment_id: str
    algorithm: str
    status: str
    training_cutoff: datetime
    available_at: datetime
    artifact: str | None
    metadata: dict


class EvaluationView(BaseModel):
    id: str
    forecast_id: str
    snapshot_id: str
    version: int
    status: str
    reason: str
    metrics: dict
