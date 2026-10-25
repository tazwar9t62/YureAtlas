"""Relational archive. Immutable entities are additionally protected by DB triggers."""

import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(UTC)


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Source(Identity, Base):
    __tablename__ = "catalogue_sources"
    name: Mapped[str] = mapped_column(String, unique=True)
    provider: Mapped[str] = mapped_column(String)
    attribution: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="uninitialized")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class Grid(Identity, Base):
    __tablename__ = "grid_versions"
    geometry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    spec: Mapped[dict] = mapped_column(JSONB)


class Cell(Base):
    __tablename__ = "grid_cells"
    grid_id: Mapped[str] = mapped_column(ForeignKey("grid_versions.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    ix: Mapped[int] = mapped_column(Integer)
    iy: Mapped[int] = mapped_column(Integer)
    area_km2: Mapped[float] = mapped_column(Float)
    geometry: Mapped[str] = mapped_column(Geometry("POLYGON", srid=4326))
    geojson: Mapped[dict] = mapped_column(JSONB)


class Experiment(Identity, Base):
    __tablename__ = "experiment_versions"
    name: Mapped[str] = mapped_column(String, unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("catalogue_sources.id"), index=True)
    grid_id: Mapped[str] = mapped_column(ForeignKey("grid_versions.id"), index=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSONB)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


class Ingestion(Identity, Base):
    __tablename__ = "ingestion_runs"
    source_id: Mapped[str] = mapped_column(ForeignKey("catalogue_sources.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String)
    report: Mapped[dict] = mapped_column(JSONB, default=dict)


class ImportManifest(Identity, Base):
    __tablename__ = "import_manifests"
    __table_args__ = (
        UniqueConstraint("source_id", "request_digest"),
        CheckConstraint('"end" > start'),
        CheckConstraint("line_count >= 0"),
        CheckConstraint("format IN ('jma', 'csv')"),
        Index("ix_import_manifests_source_namespace", "source_id", "namespace"),
        Index("ix_import_manifests_source_hash", "source_id", "raw_hash"),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("catalogue_sources.id"))
    ingestion_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), unique=True)
    namespace: Mapped[str] = mapped_column(String)
    format: Mapped[str] = mapped_column(String)
    artifact: Mapped[str] = mapped_column(String)
    raw_hash: Mapped[str] = mapped_column(String(64))
    line_count: Mapped[int] = mapped_column(Integer)
    mapping: Mapped[dict] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(String)
    coverage_declared: Mapped[bool] = mapped_column(Boolean)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_digest: Mapped[str] = mapped_column(String(64))


class Event(Identity, Base):
    __tablename__ = "earthquake_events"
    __table_args__ = (UniqueConstraint("source_id", "provider_id"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("catalogue_sources.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String)


class Revision(Identity, Base):
    __tablename__ = "earthquake_revisions"
    __table_args__ = (UniqueConstraint("event_id", "content_hash"),)
    event_id: Mapped[str] = mapped_column(ForeignKey("earthquake_events.id"), index=True)
    origin_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    longitude: Mapped[float] = mapped_column(Float)
    latitude: Mapped[float] = mapped_column(Float)
    depth_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    magnitude: Mapped[float] = mapped_column(Float)
    magnitude_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, default="earthquake", server_default="earthquake")
    selection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    hash_parent_id: Mapped[str | None] = mapped_column(ForeignKey("earthquake_revisions.id"), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_ref: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(Geometry("POINT", srid=4326))


class Snapshot(Identity, Base):
    __tablename__ = "catalogue_snapshots"
    source_id: Mapped[str] = mapped_column(ForeignKey("catalogue_sources.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy: Mapped[str] = mapped_column(String)
    revision_ids: Mapped[list] = mapped_column(JSONB)


class FeatureSet(Identity, Base):
    __tablename__ = "feature_sets"
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_versions.id"), index=True)
    schema_version: Mapped[str] = mapped_column(String)
    artifact: Mapped[str] = mapped_column(String)
    partitions: Mapped[dict] = mapped_column(JSONB)


class Model(Identity, Base):
    __tablename__ = "model_versions"
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_versions.id"), index=True)
    algorithm: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    training_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("catalogue_snapshots.id"), nullable=True)
    artifact: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class Forecast(Identity, Base):
    __tablename__ = "forecast_runs"
    __table_args__ = (UniqueConstraint("logical_key"), CheckConstraint('"end" > start'))
    logical_key: Mapped[str] = mapped_column(String)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_versions.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("catalogue_snapshots.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="published")
    provenance: Mapped[dict] = mapped_column(JSONB)


class ForecastCell(Base):
    __tablename__ = "forecast_cells"
    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 1 AND probability != 'NaN'::float8"),
    )
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecast_runs.id"), primary_key=True)
    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    probability: Mapped[float] = mapped_column(Float)
    expected_count: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str] = mapped_column(String)


class Evaluation(Identity, Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (UniqueConstraint("forecast_id", "snapshot_id"),)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecast_runs.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("catalogue_snapshots.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONB)


class EvaluationCell(Base):
    __tablename__ = "evaluation_cells"
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), primary_key=True)
    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    observed_count: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String)


class Job(Identity, Base):
    __tablename__ = "jobs"
    logical_key: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)


class Promotion(Identity, Base):
    __tablename__ = "model_promotions"
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_versions.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    reason: Mapped[str] = mapped_column(Text)


class WorkerState(Base):
    __tablename__ = "worker_state"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class SchedulerState(Base):
    """Committed scheduling decisions, independent of worker liveness."""

    __tablename__ = "scheduler_state"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (CheckConstraint("reconciled_at >= observed_at"),)
