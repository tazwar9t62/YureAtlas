from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .forecast_execution_schema import ForecastExecution
from .timeutil import utc

ForecastMode = Literal["prospective", "retrospective_revised", "synthetic_demo"]


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9_-]+$")
    provider: Literal["usgs", "jma", "synthetic"] = "usgs"
    catalogue_namespace: str | None = Field(
        default=None, min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$"
    )
    target_magnitude: float = Field(default=4, ge=0, le=9)
    input_magnitude: float = Field(default=3, ge=0, le=9)
    west: float = Field(default=122, ge=115, le=160)
    east: float = Field(default=156, ge=115, le=160)
    south: float = Field(default=20, ge=15, le=55)
    north: float = Field(default=48, ge=15, le=55)
    size_m: int = Field(default=50000, ge=50000, le=200000)
    yellow: float = Field(default=0.01, gt=0, lt=1)
    red: float = Field(default=0.05, gt=0, lt=1)
    duration_hours: Literal[24] = 24
    magnitude_policy: str = "provider_preferred"
    cutoff_minutes_before_start: Literal[30] = 30
    missing_depth_policy: Literal["retain_null"] = "retain_null"
    evaluation_policy: Literal["complete_retrieval_provisional"] = "complete_retrieval_provisional"

    @model_validator(mode="after")
    def coherent(self):
        if self.catalogue_namespace is not None and self.provider != "jma":
            raise ValueError("Catalogue namespaces are only supported for JMA local collections")
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("Invalid study envelope")
        if self.input_magnitude > self.target_magnitude or self.yellow >= self.red:
            raise ValueError("Thresholds must be ordered")
        self.magnitude_policy = {
            "usgs": "USGS preferred (mixed types)",
            "jma": "JMA J/D/d/V/v selected",
            "synthetic": "artificial magnitude",
        }[self.provider]
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
        if self.start and self.end and not 0 < (self.end - self.start).total_seconds() <= 366 * 86400 * 30:
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
    payload: dict = Field(default_factory=dict)
    execution: ForecastExecution | None = None


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


class MetricReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ForecastMode
    days: int | None = Field(default=None, ge=0, le=3650)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.start is not None or self.end is not None:
            if self.days is not None or self.start is None or self.end is None:
                raise ValueError("Choose days or both start and end; do not mix report scopes")
            self.start, self.end = utc(self.start), utc(self.end)
            if not timedelta(0) < self.end - self.start <= timedelta(days=3650):
                raise ValueError("Choose an ordered report interval of at most 3650 days")
        elif self.days is None:
            self.days = 30
        return self


class ReportLimits(BaseModel):
    max_span_days: int
    max_forecasts: int
    max_cell_rows: int
    max_grid_cells: int


class MetricReportScope(BaseModel):
    kind: Literal["relative", "explicit", "whole_experiment"]
    requested_days: int | None
    requested_start: AwareDatetime | None
    requested_end: AwareDatetime | None
    resolved_start: AwareDatetime | None
    resolved_end: AwareDatetime | None
    anchor: AwareDatetime | None
    empty_reason: Literal["no_completed_evaluated_window", "no_forecasts", "no_forecasts_in_range"] | None
    display_timezone: Literal["Asia/Tokyo"]
    containment_policy: str
    limits: ReportLimits


class ReportExperiment(BaseModel):
    id: str
    name: str
    config_hash: str
    config: dict[str, JsonValue]
    grid_id: str
    grid_hash: str
    source_id: str
    source_name: str
    source_provider: str
    synthetic: bool


class ReportPolicies(BaseModel):
    generation: str
    evaluation: str
    inclusion: str


class ReportCounts(BaseModel):
    forecast_count: int
    evaluation_count: int
    forecast_cell_rows: int
    evaluation_cell_rows: int


class PooledScore(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    n: int
    missing: int
    positives: int
    tp: int
    fp: int
    tn: int
    fn: int
    brier_sum: float
    log_loss_sum: float
    red_area: float
    eligible_area: float
    brier: float | None
    log_loss: float | None
    recall: float | None
    precision: float | None
    false_positive_rate: float | None
    red_coverage: float | None


class CalibrationBin(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    lower: float
    upper: float
    count: int
    predicted: float | None
    observed: float | None


class EvaluatedScore(PooledScore):
    average_precision: float | None = None
    calibration: list[CalibrationBin] | None = None
    clipping_epsilon: float | None = None
    confident_misses: int | None = None
    threshold: float | None = None
    source_policy: str | None = None


class ReferenceRejection(BaseModel):
    forecast_id: str
    reasons: list[str]


class PairDecision(BaseModel):
    forecast_id: str
    model_id: str
    evaluation_id: str | None
    evaluation_version: int | None
    experiment_id: str
    start: AwareDatetime
    end: AwareDatetime
    mode: ForecastMode
    grid_hash: str | None
    revision_signature: str | None
    cell_signature: str | None
    observation_signature: str | None
    status: Literal["paired", "excluded"]
    reason: str | None
    reasons: list[str]
    reference_forecast_id: str | None
    reference_model_id: str | None
    reference_evaluation_id: str | None
    reference_evaluation_version: int | None
    reference_created_at: AwareDatetime | None
    self_reference: bool
    reference_rejections: list[ReferenceRejection] = Field(
        max_length=20,
        description="First rejected references in deterministic search order; examples only, capped at 20.",
    )
    reference_rejection_limit: Literal[20]
    reference_rejection_count: int = Field(
        ge=0,
        description="All rejected references examined before the selected reference, or all if none matched.",
    )
    reference_rejections_omitted: int = Field(
        ge=0, description="Examined rejected references omitted from the bounded identity examples."
    )
    reference_rejection_reasons: dict[str, int] = Field(
        description="Complete counts by reason across examined rejections; one reference can have multiple reasons."
    )


class PairedUncertainty(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    status: Literal["available", "insufficient_data"]
    reason: (
        Literal[
            "invalid_scores",
            "invalid_windows",
            "non_24h_windows",
            "duplicate_windows",
            "nonconsecutive_windows",
            "insufficient_days",
        ]
        | None
    )
    days: int
    minimum_days: int
    block_days: int
    simulations: int
    seed: int
    paired_cell_windows: int
    mean_difference: float | None
    interval95: tuple[float, float] | None
    method_version: str
    estimand: str
    policy: str


class ModelComparison(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    model_id: str
    algorithm: str
    metrics: PooledScore
    paired_metrics: PooledScore
    reference_metrics: PooledScore
    brier_skill: float | None
    brier_skill_reason: Literal["no_pairs", "reference_brier_zero"] | None
    paired_days: int
    excluded_days: int
    paired_forecasts: int
    excluded_forecasts: int
    total_forecasts: int
    evaluated_forecasts: int
    scoreable_forecasts: int
    unevaluated_forecasts: int
    unavailable_evaluation_forecasts: int
    invalid_evidence_forecasts: int
    unavailable_forecasts: int
    total_cell_windows: int | None
    observed_cell_windows: int
    unscored_cell_windows: int | None
    missing_cell_windows: int
    paired_cell_windows: int
    paired_missing_cell_windows: int
    excluded_observed_cell_windows: int
    excluded_missing_cell_windows: int
    unknown_cell_window_forecasts: int
    uncertainty: PairedUncertainty
    reference_model_ids: list[str]
    reference_model_count: int
    reference_forecast_ids: list[str]
    reference_forecast_count: int
    reference_evaluation_ids: list[str]
    reference_evaluation_count: int
    mixed_reference_models: bool
    exclusion_reasons: dict[str, int]
    primary_exclusion_reasons: dict[str, int]
    decisions: list[PairDecision]


class MetricSeriesRow(BaseModel):
    date: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    forecast_id: str
    model_id: str
    algorithm: str
    evaluation_id: str
    evaluation_version: int
    status: str
    snapshot: str
    metrics: EvaluatedScore


class MetricReport(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    mode: ForecastMode
    days: int | None
    scope: MetricReportScope
    experiment: ReportExperiment
    policies: ReportPolicies
    counts: ReportCounts
    selected_forecast_ids: list[str]
    selected_evaluation_ids: list[str]
    policy_version: str
    score_policy_version: str
    matching_policy: str
    aggregation_policy: str
    series_policy: str
    series: list[MetricSeriesRow]
    comparisons: list[ModelComparison]
    decisions: list[PairDecision]
