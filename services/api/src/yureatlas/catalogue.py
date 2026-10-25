"""Source-isolated revisions and reproducible receipt-time snapshots."""

import hashlib
import heapq
from contextlib import nullcontext
from datetime import datetime
from itertools import pairwise

import numpy as np
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, shape
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .artifacts import canonical, digest, put, read
from .db import transaction
from .eligibility import eligible_cell, field
from .grid import StudyGrid
from .schemas import ExperimentSpec
from .source_locks import verify_usgs_writer
from .tables import (
    Cell,
    Event,
    Experiment,
    Grid,
    ImportManifest,
    Ingestion,
    Revision,
    Snapshot,
    Source,
    now,
    uid,
)
from .timeutil import utc

ATTRIBUTION = {
    "usgs": "USGS Earthquake Hazards Program · preferred catalogue magnitude, mixed types · public domain",
    "jma": "Japan Meteorological Agency, Seismological Bulletin of Japan · JMA magnitude family",
    "synthetic": "YureAtlas deterministic artificial data · no historical or physical interpretation",
}
REVISION_FIELDS = (
    "origin_time",
    "provider_updated",
    "longitude",
    "latitude",
    "depth_km",
    "magnitude",
    "magnitude_type",
    "status",
)


def revision_values(record):
    return {key: field(record, key) for key in REVISION_FIELDS}


def revision_content_hash(record, *, supersedes=None):
    """Hash normalized content; pass stored hash_parent_id to verify a linked row.

    The default deliberately ignores a row's parent when comparing its content
    with a redelivery. Legacy default earthquakes retain the original recipe.
    """
    values = revision_values(record)
    event_type = field(record, "event_type", "earthquake")
    reason = field(record, "selection_reason")
    if event_type == "earthquake" and reason is None and supersedes is None:
        return digest(values)  # Preserve the original hash recipe on legacy redelivery.
    return digest(
        {
            "version": 2,
            "values": values,
            "event_type": event_type,
            "selection_reason": reason,
            "supersedes": supersedes,
        }
    )


def discovery_and_corrections(source_id, records, grid, config, *, discovery_interval=None, session=None):
    """Keep eligible discoveries and valid corrections to already-known IDs.

    A line can leave the selection without deleting the event. An absent line
    supplies no correction. A supplied session keeps discovery and persistence
    under the caller's source lock; standalone reads use their own transaction.
    """
    discovered = {
        r["provider_id"]
        for r in records
        if eligible_cell(r, grid, config) is not None
        and (discovery_interval is None or discovery_interval[0] <= r["origin_time"] < discovery_interval[1])
    }
    candidates = sorted({r["provider_id"] for r in records} - discovered)
    known = set(discovered)
    if candidates:
        with nullcontext(session) if session is not None else transaction() as s:
            for offset in range(0, len(candidates), 1000):
                known.update(
                    s.scalars(
                        select(Event.provider_id).where(
                            Event.source_id == source_id,
                            Event.provider_id.in_(candidates[offset : offset + 1000]),
                        )
                    )
                )
    return [r for r in records if r["provider_id"] in known]


def ensure_experiment(spec: ExperimentSpec) -> Experiment:
    config = spec.model_dump(exclude_none=True)
    grid = StudyGrid(spec.west, spec.south, spec.east, spec.north, spec.size_m)
    with transaction() as s:
        s.execute(__import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(8247712)"))
        existing = s.scalar(select(Experiment).where(Experiment.name == spec.name))
        if existing:
            if existing.config_hash != digest(config):
                raise ValueError("Experiment name already belongs to a different immutable specification")
            return existing
        domain_key = digest({k: config[k] for k in ("west", "south", "east", "north", "input_magnitude")})[
            :12
        ]
        source_name = (
            f"synthetic:{spec.name}" if spec.provider == "synthetic" else f"{spec.provider}:{domain_key}"
        )
        if spec.catalogue_namespace is not None:
            source_name = f"jma:collection:{digest(spec.catalogue_namespace)}:{domain_key}"
        source = s.scalar(select(Source).where(Source.name == source_name))
        if not source:
            source = Source(
                name=source_name,
                provider=spec.provider,
                attribution=ATTRIBUTION[spec.provider],
                status="uninitialized",
                details={
                    "verified_format": True,
                    **(
                        {"catalogue_namespace": spec.catalogue_namespace}
                        if spec.catalogue_namespace is not None
                        else {}
                    ),
                },
            )
            s.add(source)
            s.flush()
        grid_row = s.scalar(select(Grid).where(Grid.geometry_hash == grid.hash))
        if not grid_row:
            grid_row = Grid(geometry_hash=grid.hash, spec=grid.spec)
            s.add(grid_row)
            s.flush()
            s.execute(
                insert(Cell),
                [
                    dict(c, grid_id=grid_row.id, geometry=from_shape(shape(c["geojson"]), srid=4326))
                    for c in grid.cells
                ],
            )
        experiment = Experiment(
            name=spec.name,
            source_id=source.id,
            grid_id=grid_row.id,
            config_hash=digest(config),
            config=config,
            synthetic=spec.provider == "synthetic",
        )
        s.add(experiment)
        s.flush()
        return experiment


def load_experiment(experiment_id: str):
    with transaction() as s:
        experiment = s.get(Experiment, experiment_id)
        if not experiment:
            raise ValueError("Unknown experiment")
        return experiment, StudyGrid.from_spec(s.get(Grid, experiment.grid_id).spec)


def prepare_import_manifest(session, source, identity, start, end):
    """Validate immutable import identity while the caller holds the source lock.

    No manifest, ingestion or revision may be committed separately. The request
    hash is derived here rather than accepted from a queued job's payload.
    """
    collection = source.details.get("catalogue_namespace")
    if source.provider != "jma" or not collection or identity.get("catalogue_namespace") != collection:
        raise ValueError("Local imports require their experiment's explicit JMA catalogue namespace")
    namespace = identity.get("namespace")
    parser_version = identity.get("parser_version")
    format_name = identity.get("format")
    mapping = identity.get("mapping", {})
    line_count = identity.get("line_count")
    coverage_declared = identity.get("coverage_declared")
    if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 80:
        raise ValueError("An import requires a stable file namespace of at most 80 characters")
    if format_name not in {"jma", "csv"} or not isinstance(parser_version, str) or not parser_version:
        raise ValueError("An import requires its format and parser version")
    if not isinstance(mapping, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
    ):
        raise ValueError("CSV mapping must map column names to canonical column names")
    if format_name == "jma" and mapping:
        raise ValueError("Fixed-width JMA imports cannot use a CSV mapping")
    if type(line_count) is not int or line_count < 0 or not isinstance(coverage_declared, bool):
        raise ValueError("An import requires its physical line count and explicit coverage declaration")
    start, end = utc(start), utc(end)
    if start >= end:
        raise ValueError("An import requires an ordered half-open interval")
    artifact = identity.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError("An import requires a verified raw artifact")
    raw = read(artifact)
    if len(raw.splitlines()) != line_count:
        raise ValueError("Import line count does not match its verified raw artifact")
    values = {
        "namespace": namespace,
        "format": format_name,
        "artifact": artifact,
        "raw_hash": hashlib.sha256(raw).hexdigest(),
        "line_count": line_count,
        "mapping": mapping,
        "parser_version": parser_version,
        "coverage_declared": coverage_declared,
        "start": start,
        "end": end,
    }
    request_digest = digest(dict(values, catalogue_namespace=collection))
    previous = session.scalar(
        select(ImportManifest).where(
            ImportManifest.source_id == source.id, ImportManifest.request_digest == request_digest
        )
    )
    if previous:
        return None, dict(session.get(Ingestion, previous.ingestion_id).report, idempotent_replay=True)
    existing_format = session.scalar(
        select(ImportManifest.format).where(ImportManifest.source_id == source.id).limit(1)
    )
    if existing_format is None:
        if session.scalar(select(Event.id).where(Event.source_id == source.id).limit(1)) or session.scalar(
            select(Ingestion.id).where(Ingestion.source_id == source.id).limit(1)
        ):
            raise ValueError("Existing untracked catalogue data cannot be adopted into a local collection")
    elif existing_format != format_name:
        raise ValueError("A local catalogue collection cannot mix fixed-width JMA and CSV identity")
    if format_name == "jma":
        prior_file = session.scalar(
            select(ImportManifest)
            .where(ImportManifest.source_id == source.id, ImportManifest.namespace == namespace)
            .limit(1)
        )
        if prior_file and (prior_file.raw_hash != values["raw_hash"] or prior_file.line_count != line_count):
            raise ValueError(
                "Fixed-width file namespace is immutable; changed or reordered content is refused"
            )
        conflict = session.scalar(
            select(ImportManifest.id)
            .where(
                ImportManifest.source_id == source.id,
                ImportManifest.namespace != namespace,
                (
                    (ImportManifest.start < end) & (ImportManifest.end > start)
                    | (ImportManifest.raw_hash == values["raw_hash"])
                ),
            )
            .limit(1)
        )
        if conflict:
            raise ValueError(
                "Distinct fixed-width namespaces cannot overlap intervals or reuse identical content"
            )
    return dict(values, source_id=source.id, request_digest=request_digest), None


def ingest_records(
    source_id: str,
    records: list[dict],
    start: datetime,
    end: datetime,
    report: dict,
    received_at: datetime | None = None,
    status="complete",
    *,
    import_identity: dict | None = None,
    discovery_context: dict | None = None,
    attempt_owner=None,
) -> dict:
    receipt = received_at or now()
    delivery_versions = {}
    for record in records:
        version = (record["provider_id"], record.get("provider_updated"))
        checksum = revision_content_hash(record)
        if version in delivery_versions and delivery_versions[version] != checksum:
            raise ValueError(
                "Conflicting records for one event/revision in a delivery; "
                "import ordered revisions separately when provider update times are absent"
            )
        delivery_versions[version] = checksum
    added = 0
    duplicate = 0
    with transaction() as s:
        s.execute(
            __import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": source_id}
        )
        source = s.get(Source, source_id)
        if source is None:
            raise ValueError("Unknown catalogue source")
        if source.provider == "usgs":
            verify_usgs_writer(s, source_id, attempt_owner)
        elif attempt_owner is not None:
            raise ValueError("USGS attempt ownership cannot authorize another provider")
        manifest_values = None
        if import_identity is not None:
            if status == "complete" and import_identity.get("coverage_declared") is not True:
                raise ValueError("Complete local coverage requires an explicit coverage declaration")
            manifest_values, replay = prepare_import_manifest(s, source, import_identity, start, end)
            if replay is not None:
                return replay
        elif source.provider == "jma":
            raise ValueError("New local imports require an explicit catalogue namespace and import identity")
        capture_ids = False
        if discovery_context is not None:
            context = dict(discovery_context)
            capture_ids = context.pop("capture_ids", False)
            records = discovery_and_corrections(source_id, records, session=s, **context)
        ids = sorted({r["provider_id"] for r in records})
        events = {}
        for offset in range(0, len(ids), 1000):
            events.update(
                {
                    e.provider_id: e
                    for e in s.scalars(
                        select(Event).where(
                            Event.source_id == source_id, Event.provider_id.in_(ids[offset : offset + 1000])
                        )
                    )
                }
            )
        # Sources without provider revision times explicitly use receipt ordering.
        # Compare the latest content so A -> B -> A appends, while A -> A deduplicates.
        receipt_ids = sorted(
            {
                events[r["provider_id"]].id
                for r in records
                if r.get("provider_updated") is None and r["provider_id"] in events
            }
        )
        latest = {}
        for offset in range(0, len(receipt_ids), 1000):
            rows = s.scalars(
                select(Revision)
                .where(Revision.event_id.in_(receipt_ids[offset : offset + 1000]))
                .distinct(Revision.event_id)
                .order_by(
                    Revision.event_id,
                    func.coalesce(Revision.provider_updated, Revision.received_at).desc(),
                    Revision.received_at.desc(),
                    Revision.id.desc(),
                )
            )
            latest.update({r.event_id: r for r in rows})
        for record in records:
            event = events.get(record["provider_id"])
            if not event:
                event = Event(source_id=source_id, provider_id=record["provider_id"], created_at=receipt)
                s.add(event)
                s.flush()
                events[event.provider_id] = event
            values = revision_values(record)
            event_type = record.get("event_type", "earthquake")
            selection_reason = record.get("selection_reason")
            previous = latest.get(event.id)
            checksum = revision_content_hash(record)
            hash_parent_id = None
            if values["provider_updated"] is None and previous is not None:
                if checksum == revision_content_hash(previous):
                    duplicate += 1
                    continue
                # Linking the preceding immutable revision also distinguishes a
                # return to older content from its original receipt-time version.
                hash_parent_id = field(previous, "id")
                checksum = revision_content_hash(record, supersedes=hash_parent_id)
            raw_ref = put(canonical(record.get("raw", record)), "json")
            revision_id = uid()
            inserted = s.execute(
                insert(Revision)
                .values(
                    id=revision_id,
                    created_at=receipt,
                    event_id=event.id,
                    received_at=receipt,
                    content_hash=checksum,
                    event_type=event_type,
                    selection_reason=selection_reason,
                    hash_parent_id=hash_parent_id,
                    raw_ref=raw_ref,
                    location=from_shape(Point(values["longitude"], values["latitude"]), srid=4326),
                    **values,
                )
                .on_conflict_do_nothing(index_elements=[Revision.event_id, Revision.content_hash])
                .returning(Revision.id)
            ).scalar_one_or_none()
            added += int(inserted is not None)
            duplicate += int(inserted is None)
            if inserted is not None:
                incoming_order = (values["provider_updated"] or receipt, receipt, revision_id)
                previous_order = (
                    (
                        field(previous, "provider_updated") or field(previous, "received_at"),
                        field(previous, "received_at"),
                        field(previous, "id"),
                    )
                    if previous is not None
                    else None
                )
                if previous_order is None or incoming_order > previous_order:
                    latest[event.id] = dict(
                        values,
                        id=revision_id,
                        received_at=receipt,
                        event_type=event_type,
                        selection_reason=selection_reason,
                    )
        final_report = dict(report, added=added, duplicate=duplicate, received_at=receipt.isoformat())
        if discovery_context is not None:
            final_report["persisted_candidates"] = len(records)
        if capture_ids:
            final_report["persisted_provider_ids"] = ids
        ingestion_id = uid()
        if manifest_values is not None:
            manifest_id = uid()
            final_report.update(
                import_manifest_id=manifest_id, request_digest=manifest_values["request_digest"]
            )
        s.add(
            Ingestion(
                id=ingestion_id,
                source_id=source_id,
                start=start,
                end=end,
                status=status,
                report=final_report,
                created_at=receipt,
            )
        )
        if manifest_values is not None:
            s.flush()
            s.add(
                ImportManifest(
                    id=manifest_id, ingestion_id=ingestion_id, created_at=receipt, **manifest_values
                )
            )
        source.status = "healthy" if status == "complete" else status
        source.details = dict(
            source.details,
            last_report=final_report,
            last_success=receipt.isoformat() if status == "complete" else source.details.get("last_success"),
        )
    return final_report


def revisions_asof(source_id: str, cutoff: datetime, *, origin_before: datetime | None = None):
    with transaction() as s:
        # Provider update time wins over an out-of-order delivery of an older version.
        # Receipt time still strictly bounds which versions could have been known.
        ranked = (
            select(
                Revision.id,
                func.row_number()
                .over(
                    partition_by=Revision.event_id,
                    order_by=(
                        func.coalesce(Revision.provider_updated, Revision.received_at).desc(),
                        Revision.received_at.desc(),
                        Revision.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .join(Event, Event.id == Revision.event_id)
            .where(Event.source_id == source_id, Revision.received_at <= cutoff)
            .subquery()
        )
        query = select(Revision).join(ranked, ranked.c.id == Revision.id).where(ranked.c.rank == 1)
        # Filter origin/status AFTER choosing the revision, so a moved/deleted event
        # cannot resurrect an older version within the target interval.
        rows = list(s.scalars(query.order_by(Revision.origin_time, Revision.id)))
        return [
            r
            for r in rows
            if r.status != "deleted" and (origin_before is None or r.origin_time < origin_before)
        ]


def make_snapshot(
    source_id: str, cutoff: datetime, policy: str, *, origin_before=None, origin_after=None
) -> tuple[Snapshot, list[Revision]]:
    rows = revisions_asof(source_id, cutoff)
    rows = [
        r
        for r in rows
        if (origin_before is None or r.origin_time < origin_before)
        and (origin_after is None or r.origin_time >= origin_after)
    ]
    revision_ids = sorted(r.id for r in rows)
    checksum = digest(
        {
            "source": source_id,
            "revisions": revision_ids,
            "policy": policy,
            "cutoff": cutoff,
            "origin_before": origin_before,
            "origin_after": origin_after,
        }
    )
    with transaction() as s:
        s.execute(
            insert(Snapshot)
            .values(
                id=uid(),
                created_at=now(),
                source_id=source_id,
                digest=checksum,
                cutoff=cutoff,
                policy=policy,
                revision_ids=revision_ids,
            )
            .on_conflict_do_nothing(index_elements=[Snapshot.digest])
        )
        return s.scalar(select(Snapshot).where(Snapshot.digest == checksum)), rows


def coverage_complete(runs, start: datetime, end: datetime) -> bool:
    """Overlay authoritative attempts; later uncertainty supersedes earlier coverage.

    Feed deliveries and individual correction receipts are supplemental evidence,
    not attempts to establish interval coverage. For each elementary interval the
    latest authoritative attempt must be complete. Equal receipt-time ambiguity
    fails closed. The sweep is O(n log n), without expanding into time bins.
    """
    if start >= end:
        return False
    attempts = sorted(
        (
            r
            for r in runs
            if r.start < end
            and r.end > start
            and r.start < r.end
            and not r.report.get("feed")
            and not r.report.get("synthetic_revision")
            and r.report.get("coverage_effect") != "supplement"
        ),
        key=lambda r: (r.created_at, r.status != "complete", r.id),
    )
    boundaries = {start: [], end: []}
    for index, run in enumerate(attempts):
        boundaries.setdefault(max(start, run.start), []).append((index, True))
        boundaries.setdefault(min(end, run.end), []).append((index, False))
    active = set()
    latest = []
    for left, _right in pairwise(sorted(boundaries)):
        for index, begins in boundaries[left]:
            if begins:
                active.add(index)
                heapq.heappush(latest, -index)
            else:
                active.discard(index)
        while latest and -latest[0] not in active:
            heapq.heappop(latest)
        if not latest or attempts[-latest[0]].status != "complete":
            return False
    return True


def has_coverage(source_id: str, start: datetime, end: datetime, cutoff: datetime | None = None) -> bool:
    with transaction() as s:
        # Coverage checks run for many model windows; do not load every raw
        # request manifest and rejection sample just to inspect three flags.
        query = select(
            Ingestion.id,
            Ingestion.start,
            Ingestion.end,
            Ingestion.created_at,
            Ingestion.status,
            func.jsonb_build_object(
                "feed",
                Ingestion.report["feed"],
                "synthetic_revision",
                Ingestion.report["synthetic_revision"],
                "coverage_effect",
                Ingestion.report["coverage_effect"],
            ).label("report"),
        ).where(
            Ingestion.source_id == source_id,
            Ingestion.start < end,
            Ingestion.end > start,
        )
        if cutoff is not None:
            query = query.where(Ingestion.created_at <= cutoff)
        runs = s.execute(query).all()
    return coverage_complete(runs, start, end)


def event_arrays(rows, grid: StudyGrid, config: dict):
    eligible = [(r, eligible_cell(r, grid, config)) for r in rows]
    eligible = [(r, cell) for r, cell in eligible if cell is not None]
    return {
        "t": np.array([r.origin_time.timestamp() / 86400 for r, _ in eligible]),
        "cell": np.array([grid.lookup[cell] for _, cell in eligible], dtype=int),
        "m": np.array([r.magnitude for r, _ in eligible]),
        "depth": np.array([r.depth_km if r.depth_km is not None else np.nan for r, _ in eligible]),
    }
