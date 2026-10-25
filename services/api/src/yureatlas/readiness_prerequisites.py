"""Bounded advisory evidence, separate from infrastructure readiness.

The caller owns the Session and its read-only transaction/time budgets. No
catalogue coverage reconstruction, artifact reads or model deserialization occur.
"""

from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select, true

from .forecast_execution import SELECTION_POLICY, select_forecast_model
from .schedule_configuration import load_schedule
from .tables import Experiment, Ingestion, Source
from .timeutil import utc

INSPECTION_LIMIT = 20
CAVEAT = (
    "Advisory metadata only. The latest receipt does not establish completeness of any required "
    "interval, and model metadata does not verify artifact integrity, source freshness, or predictive "
    "readiness. Forecast execution performs its own checks."
)


class PrerequisiteReceipt(BaseModel):
    id: str
    received_at: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    status: str
    coverage_effect: str | None = None
    feed: bool = False


class SourcePrerequisite(BaseModel):
    source_id: str
    provider: str
    status: Literal["no_receipt", "receipt_observed"]
    latest_receipt: PrerequisiteReceipt | None = None
    coverage_verified: Literal[False] = False
    message: str


class ModelPrerequisite(BaseModel):
    status: Literal["metadata_available", "unavailable"]
    policy: str = SELECTION_POLICY
    kind: Literal["promotion", "historical_rate"] | None = None
    model_id: str | None = None
    algorithm: str | None = None
    training_cutoff: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    promotion_id: str | None = None
    message: str


class ExperimentPrerequisites(BaseModel):
    experiment_id: str
    name: str
    attention_needed: bool
    source: SourcePrerequisite
    model: ModelPrerequisite


class ReadinessPrerequisites(BaseModel):
    status: Literal[
        "unavailable",
        "unknown",
        "first_run",
        "real_setup_needed",
        "empty_scope",
        "needs_attention",
        "metadata_available",
        "sampled",
    ]
    message: str
    advisory: Literal[True] = True
    caveat: str = CAVEAT
    checked_at: AwareDatetime | None = None
    schedule_revision: int | None = None
    scope: Literal["all_real", "selected"] | None = None
    selected_experiment_ids: list[str] = Field(default_factory=list, max_length=100)
    inspection_limit: Literal[20] = INSPECTION_LIMIT
    inspected_count: int = Field(default=0, ge=0, le=INSPECTION_LIMIT)
    has_more: bool | None = None
    scope_complete: bool | None = None
    selection_start: AwareDatetime | None = None
    selection_input_cutoff: AwareDatetime | None = None
    experiments: list[ExperimentPrerequisites] = Field(default_factory=list, max_length=INSPECTION_LIMIT)


def _latest_receipts(session, source_ids, checked_at):
    """One top-one lateral lookup per bounded source, without loading reports."""
    latest = (
        select(
            Ingestion.id,
            Ingestion.created_at.label("received_at"),
            Ingestion.start,
            Ingestion.end,
            Ingestion.status,
            Ingestion.report["coverage_effect"].as_string().label("coverage_effect"),
            Ingestion.report["feed"].as_string().label("feed"),
        )
        .where(Ingestion.source_id == Source.id, Ingestion.created_at <= checked_at)
        .order_by(Ingestion.created_at.desc(), Ingestion.id.desc())
        .limit(1)
        .correlate(Source)
        .lateral()
    )
    query = (
        select(Source.id.label("source_id"), latest)
        .select_from(Source)
        .outerjoin(latest, true())
        .where(Source.id.in_(sorted(source_ids)))
    )
    return {
        row["source_id"]: (
            PrerequisiteReceipt(
                id=row["id"],
                received_at=row["received_at"],
                start=row["start"],
                end=row["end"],
                status=row["status"],
                coverage_effect=row["coverage_effect"],
                feed=row["feed"] == "true",
            )
            if row["id"] is not None
            else None
        )
        for row in session.execute(query).mappings()
    }


def _source_evidence(source_id, provider, receipt):
    if receipt is None:
        message = "No ingestion receipt is recorded by the check time; import or retrieve real source data."
    elif receipt.coverage_effect == "supplement" or receipt.feed:
        message = "The latest receipt is supplemental evidence; it does not declare interval completeness."
    elif receipt.status == "complete":
        message = "The latest receipt reports complete; coverage of a required interval is not verified here."
    else:
        message = (
            "The latest receipt is not complete; inspect source diagnostics and required interval coverage."
        )
    return SourcePrerequisite(
        source_id=source_id,
        provider=provider,
        status="no_receipt" if receipt is None else "receipt_observed",
        latest_receipt=receipt,
        message=message,
    )


def _model_evidence(session, experiment_id, checked_at):
    try:
        model, selection = select_forecast_model(session, experiment_id, None, checked_at, checked_at)
    except ValueError as error:
        # The shared selector emits bounded application messages. Unexpected DB
        # failures propagate to the caller's infrastructure probe.
        return ModelPrerequisite(status="unavailable", message=str(error))
    return ModelPrerequisite(
        status="metadata_available",
        kind=selection["kind"],
        model_id=model.id,
        algorithm=model.algorithm,
        training_cutoff=model.training_cutoff,
        available_at=model.available_at,
        promotion_id=selection["promotion_id"],
        message="Automatic selection accepts archived model metadata; artifact bytes were not inspected.",
    )


def inspect_prerequisites(session, checked_at) -> ReadinessPrerequisites:
    """Inspect at most 20 requested real experiments using the caller's Session.

    Persisted enablement and kind switches do not hide setup guidance. Scope
    completeness describes only this metadata sample, never catalogue coverage.
    """
    checked_at = utc(checked_at)
    configuration = load_schedule(session)
    spec = configuration["spec"]
    query = (
        select(Experiment.id, Experiment.name, Experiment.source_id, Source.provider)
        .join(Source, Source.id == Experiment.source_id)
        .where(Experiment.synthetic == False)
        .order_by(Experiment.created_at, Experiment.id)
        .limit(INSPECTION_LIMIT + 1)
    )
    if spec["scope"] == "selected":
        query = query.where(Experiment.id.in_(spec["experiment_ids"]))
    rows = list(session.execute(query).mappings())
    truncated = len(rows) > INSPECTION_LIMIT
    rows = rows[:INSPECTION_LIMIT]
    base = {
        "checked_at": checked_at,
        "schedule_revision": configuration["revision"],
        "scope": spec["scope"],
        "selected_experiment_ids": spec["experiment_ids"],
        "has_more": truncated,
        "scope_complete": not truncated,
        "selection_start": checked_at + timedelta(minutes=30),
        "selection_input_cutoff": checked_at,
    }
    if not rows:
        any_experiment, any_real = session.execute(
            select(
                select(Experiment.id).exists(),
                select(Experiment.id).where(Experiment.synthetic == False).exists(),
            )
        ).one()
        if not any_experiment:
            status, message = "first_run", "Create a real experiment, import or retrieve history, then train."
        elif not any_real:
            status, message = (
                "real_setup_needed",
                "Only synthetic experiments are present; create a real experiment for prospective work.",
            )
        else:
            status, message = (
                "empty_scope",
                "No real experiments are included in the requested schedule scope.",
            )
        return ReadinessPrerequisites(**base, status=status, message=message)

    receipts = _latest_receipts(session, {row["source_id"] for row in rows}, checked_at)
    items = []
    for row in rows:
        receipt = receipts.get(row["source_id"])
        model = _model_evidence(session, row["id"], checked_at)
        attention = (
            receipt is None
            or receipt.status != "complete"
            or receipt.coverage_effect == "supplement"
            or receipt.feed
            or model.status == "unavailable"
        )
        items.append(
            ExperimentPrerequisites(
                experiment_id=row["id"],
                name=row["name"],
                attention_needed=attention,
                source=_source_evidence(row["source_id"], row["provider"], receipt),
                model=model,
            )
        )
    if any(item.attention_needed for item in items):
        status, message = (
            "needs_attention",
            "Inspect the reported source/model prerequisites before forecasting.",
        )
    elif truncated:
        status, message = (
            "sampled",
            "Only the first 20 experiments were inspected; remaining prerequisites are unknown.",
        )
    else:
        status, message = (
            "metadata_available",
            "Inspected experiments have complete latest receipts and selectable model metadata; execution checks remain.",
        )
    return ReadinessPrerequisites(
        **base,
        status=status,
        message=message,
        inspected_count=len(items),
        experiments=items,
    )
