"""Source-isolated revisions and reproducible receipt-time snapshots."""
from datetime import datetime

import numpy as np
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, shape
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .artifacts import canonical, digest, put
from .db import transaction
from .grid import StudyGrid
from .schemas import ExperimentSpec
from .tables import Cell, Event, Experiment, Grid, Ingestion, Revision, Snapshot, Source, now, uid

ATTRIBUTION = {
    "usgs": "USGS Earthquake Hazards Program · preferred catalogue magnitude, mixed types · public domain",
    "jma": "Japan Meteorological Agency, Seismological Bulletin of Japan · JMA magnitude family",
    "synthetic": "YureAtlas deterministic artificial data · no historical or physical interpretation",
}


def ensure_experiment(spec: ExperimentSpec) -> Experiment:
    config = spec.model_dump()
    grid = StudyGrid(spec.west, spec.south, spec.east, spec.north, spec.size_m)
    with transaction() as s:
        s.execute(__import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(8247712)"))
        existing = s.scalar(select(Experiment).where(Experiment.name == spec.name))
        if existing:
            if existing.config_hash != digest(config):
                raise ValueError("Experiment name already belongs to a different immutable specification")
            return existing
        domain_key = digest({k: config[k] for k in ("west", "south", "east", "north", "input_magnitude")})[:12]
        source_name = f"synthetic:{spec.name}" if spec.provider == "synthetic" else f"{spec.provider}:{domain_key}"
        source = s.scalar(select(Source).where(Source.name == source_name))
        if not source:
            source = Source(name=source_name, provider=spec.provider, attribution=ATTRIBUTION[spec.provider],
                            status="uninitialized", details={"verified_format": True})
            s.add(source)
            s.flush()
        grid_row = s.scalar(select(Grid).where(Grid.geometry_hash == grid.hash))
        if not grid_row:
            grid_row = Grid(geometry_hash=grid.hash, spec=grid.spec)
            s.add(grid_row)
            s.flush()
            s.execute(insert(Cell), [dict(c, grid_id=grid_row.id, geometry=from_shape(shape(c["geojson"]), srid=4326))
                                     for c in grid.cells])
        experiment = Experiment(name=spec.name, source_id=source.id, grid_id=grid_row.id,
                                config_hash=digest(config), config=config, synthetic=spec.provider == "synthetic")
        s.add(experiment)
        s.flush()
        return experiment


def load_experiment(experiment_id: str):
    with transaction() as s:
        experiment = s.get(Experiment, experiment_id)
        if not experiment:
            raise ValueError("Unknown experiment")
        return experiment, StudyGrid.from_spec(s.get(Grid, experiment.grid_id).spec)


def ingest_records(source_id: str, records: list[dict], start: datetime, end: datetime,
                   report: dict, received_at: datetime | None = None, status="complete") -> dict:
    receipt = received_at or now()
    added = 0
    duplicate = 0
    with transaction() as s:
        s.execute(__import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": source_id})
        ids = [r["provider_id"] for r in records]
        events = {e.provider_id: e for e in s.scalars(select(Event).where(Event.source_id == source_id,
                                                                         Event.provider_id.in_(ids)))}
        for record in records:
            event = events.get(record["provider_id"])
            if not event:
                event = Event(source_id=source_id, provider_id=record["provider_id"], created_at=receipt)
                s.add(event)
                s.flush()
                events[event.provider_id] = event
            values = {k: record.get(k) for k in ("origin_time", "provider_updated", "longitude", "latitude",
                       "depth_km", "magnitude", "magnitude_type", "status")}
            raw_ref = put(canonical(record.get("raw", record)), "json")
            inserted = s.execute(insert(Revision).values(
                id=uid(), created_at=receipt, event_id=event.id, received_at=receipt,
                content_hash=digest(values), raw_ref=raw_ref,
                location=from_shape(Point(values["longitude"], values["latitude"]), srid=4326), **values)
                .on_conflict_do_nothing(index_elements=[Revision.event_id, Revision.content_hash])
                .returning(Revision.id)).scalar_one_or_none()
            added += int(inserted is not None)
            duplicate += int(inserted is None)
        final_report = dict(report, added=added, duplicate=duplicate, received_at=receipt.isoformat())
        s.add(Ingestion(source_id=source_id, start=start, end=end, status=status, report=final_report,
                        created_at=receipt))
        source = s.get(Source, source_id)
        source.status = "healthy" if status == "complete" else status
        source.details = dict(source.details, last_report=final_report, last_success=receipt.isoformat()
                              if status == "complete" else source.details.get("last_success"))
    return final_report


def revisions_asof(source_id: str, cutoff: datetime, *, origin_before: datetime | None = None):
    with transaction() as s:
        # Provider update time wins over an out-of-order delivery of an older version.
        # Receipt time still strictly bounds which versions could have been known.
        ranked = select(Revision.id, func.row_number().over(
            partition_by=Revision.event_id,
            order_by=(func.coalesce(Revision.provider_updated, Revision.received_at).desc(),
                      Revision.received_at.desc(), Revision.id.desc())).label("rank"))\
            .join(Event, Event.id == Revision.event_id)\
            .where(Event.source_id == source_id, Revision.received_at <= cutoff).subquery()
        query = select(Revision).join(ranked, ranked.c.id == Revision.id).where(ranked.c.rank == 1)
        # Filter origin/status AFTER choosing the revision, so a moved/deleted event
        # cannot resurrect an older version within the target interval.
        rows = list(s.scalars(query.order_by(Revision.origin_time, Revision.id)))
        return [r for r in rows if r.status != "deleted" and (origin_before is None or r.origin_time < origin_before)]


def make_snapshot(source_id: str, cutoff: datetime, policy: str) -> tuple[Snapshot, list[Revision]]:
    rows = revisions_asof(source_id, cutoff)
    revision_ids = sorted(r.id for r in rows)
    checksum = digest({"source": source_id, "revisions": revision_ids, "policy": policy, "cutoff": cutoff})
    with transaction() as s:
        s.execute(insert(Snapshot).values(id=uid(), created_at=now(), source_id=source_id, digest=checksum,
                                         cutoff=cutoff, policy=policy, revision_ids=revision_ids)
                  .on_conflict_do_nothing(index_elements=[Snapshot.digest]))
        return s.scalar(select(Snapshot).where(Snapshot.digest == checksum)), rows


def has_coverage(source_id: str, start: datetime, end: datetime, cutoff: datetime | None = None) -> bool:
    with transaction() as s:
        query = select(Ingestion).where(Ingestion.source_id == source_id, Ingestion.status == "complete",
                                        Ingestion.start < end, Ingestion.end > start)
        if cutoff:
            query = query.where(Ingestion.created_at <= cutoff)
        runs = s.scalars(query.order_by(Ingestion.start)).all()
    cursor = start
    for run in runs:
        if run.start > cursor:
            break
        cursor = max(cursor, run.end)
        if cursor >= end:
            return True
    return False


def event_arrays(rows, grid: StudyGrid, min_magnitude: float):
    eligible = [(r, grid.assign(r.longitude, r.latitude)) for r in rows if r.magnitude >= min_magnitude]
    eligible = [(r, cell) for r, cell in eligible if cell is not None]
    return {
        "t": np.array([r.origin_time.timestamp()/86400 for r, _ in eligible]),
        "cell": np.array([grid.lookup[cell] for _, cell in eligible], dtype=int),
        "m": np.array([r.magnitude for r, _ in eligible]),
        "depth": np.array([r.depth_km if r.depth_km is not None else np.nan for r, _ in eligible]),
    }
