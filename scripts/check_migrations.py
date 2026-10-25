"""Verify a populated schema002 archive through forward migrations in an owned database.

Run through scripts/test_backend.py --migration-check. This module never creates,
drops, or cleans up resources; the existing harness owns their entire lifecycle.
The seed uses explicit frozen002 columns and hash recipes, not evolving ORM models.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from scripts.test_backend import CONNECT_ARGS, ROOT, checked_url, validate_isolation

BASE = datetime(2020, 1, 1, tzinfo=UTC)
TABLE_KEYS = {
    "catalogue_sources": ("id",),
    "grid_versions": ("id",),
    "grid_cells": ("grid_id", "id"),
    "experiment_versions": ("id",),
    "earthquake_events": ("id",),
    "earthquake_revisions": ("id",),
    "ingestion_runs": ("id",),
    "catalogue_snapshots": ("id",),
    "feature_sets": ("id",),
    "model_versions": ("id",),
    "forecast_runs": ("id",),
    "forecast_cells": ("forecast_id", "cell_id"),
    "evaluation_runs": ("id",),
    "evaluation_cells": ("evaluation_id", "cell_id"),
    "model_promotions": ("id",),
    "jobs": ("id",),
    "worker_state": ("id",),
}
JSON_COLUMNS = {
    "details",
    "spec",
    "geojson",
    "config",
    "report",
    "revision_ids",
    "partitions",
    "metadata",
    "provenance",
    "metrics",
    "payload",
    "result",
    "execution",
}
CONFIG_002 = {
    "name": "migration_legacy",
    "provider": "synthetic",
    "target_magnitude": 4.0,
    "input_magnitude": 3.0,
    "west": 140.0,
    "east": 140.01,
    "south": 35.0,
    "north": 35.01,
    "size_m": 50000,
    "yellow": 0.01,
    "red": 0.05,
    "duration_hours": 24,
    "magnitude_policy": "artificial magnitude",
    "cutoff_minutes_before_start": 30,
    "missing_depth_policy": "retain_null",
    "evaluation_policy": "complete_retrieval_provisional",
}


def day(value):
    return BASE + timedelta(days=value)


def frozen_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def frozen_digest(value):
    return hashlib.sha256(frozen_json(value)).hexdigest()


def insert_row(connection, table, values):
    # Table/column identifiers are fixed source literals, never user input.
    columns, expressions, parameters = [], [], {}
    for key, value in values.items():
        columns.append(f'"{key}"')
        parameters[key] = json.dumps(value) if key in JSON_COLUMNS else value
        expressions.append(
            f"CAST(:{key} AS JSONB)"
            if key in JSON_COLUMNS
            else f"ST_GeomFromText(:{key}, 4326)"
            if key in {"location", "geometry"}
            else f":{key}"
        )
    connection.execute(
        text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(expressions)})"), parameters
    )


def seed_002(engine, artifact_dir):
    artifacts = {}

    def artifact(content, suffix):
        checksum = hashlib.sha256(content).hexdigest()
        ref = f"{checksum[:2]}/{checksum}.{suffix}"
        path = artifact_dir / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts[ref] = content
        return ref

    records = [
        {
            "provider_id": f"legacy-{kind}",
            "origin_time": day(origin),
            "provider_updated": day(1) if kind == "updated" else None,
            "longitude": 140.005,
            "latitude": 35.005,
            "depth_km": None,
            "magnitude": 4.2,
            "magnitude_type": "synthetic",
            "status": "reviewed",
        }
        for kind, origin in (("updated", 0), ("receipt", 10))
    ]
    raw_refs = [artifact(frozen_json(record), "json") for record in records]
    feature_ref = artifact(b"frozen schema002 feature artifact\x00\x01", "parquet")
    model_ref = artifact(b"frozen schema002 fitted model artifact\x00\x02", "joblib")
    polygon = [[140, 35], [140.01, 35], [140.01, 35.01], [140, 35.01], [140, 35]]
    with engine.begin() as connection:
        insert_row(
            connection,
            "catalogue_sources",
            {
                "id": "legacy-source",
                "created_at": day(0),
                "name": "synthetic:migration_legacy",
                "provider": "synthetic",
                "attribution": "Artificial migration fixture",
                "status": "healthy",
                "details": {"verified_format": True, "legacy_marker": [1, "preserve"]},
            },
        )
        insert_row(
            connection,
            "grid_versions",
            {
                "id": "legacy-grid",
                "created_at": day(0),
                "geometry_hash": frozen_digest(polygon),
                "spec": {k: CONFIG_002[k] for k in ("west", "east", "south", "north", "size_m")},
            },
        )
        insert_row(
            connection,
            "grid_cells",
            {
                "grid_id": "legacy-grid",
                "id": "legacy-cell",
                "ix": 0,
                "iy": 0,
                "area_km2": 1.01,
                "geometry": "POLYGON((140 35,140.01 35,140.01 35.01,140 35.01,140 35))",
                "geojson": {"type": "Polygon", "coordinates": [polygon]},
            },
        )
        insert_row(
            connection,
            "experiment_versions",
            {
                "id": "legacy-experiment",
                "created_at": day(0),
                "name": CONFIG_002["name"],
                "source_id": "legacy-source",
                "grid_id": "legacy-grid",
                "config_hash": frozen_digest(CONFIG_002),
                "config": CONFIG_002,
                "synthetic": True,
            },
        )
        for index, record in enumerate(records):
            values = {key: value for key, value in record.items() if key != "provider_id"}
            insert_row(
                connection,
                "earthquake_events",
                {
                    "id": f"legacy-event-{index}",
                    "created_at": day(2 + index * 10),
                    "source_id": "legacy-source",
                    "provider_id": record["provider_id"],
                },
            )
            insert_row(
                connection,
                "earthquake_revisions",
                {
                    "id": f"legacy-revision-{index}",
                    "created_at": day(2 + index * 10),
                    "event_id": f"legacy-event-{index}",
                    "received_at": day(2 + index * 10),
                    **values,
                    "content_hash": frozen_digest(values),
                    "raw_ref": raw_refs[index],
                    "location": "POINT(140.005 35.005)",
                },
            )
        insert_row(
            connection,
            "ingestion_runs",
            {
                "id": "legacy-ingestion",
                "created_at": day(13),
                "source_id": "legacy-source",
                "start": day(0),
                "end": day(12),
                "status": "complete",
                "report": {"raw_refs": raw_refs, "count": 2},
            },
        )
        for label, cutoff, revisions in (
            ("training", day(3), ["legacy-revision-0"]),
            ("evaluation", day(13), ["legacy-revision-1"]),
        ):
            insert_row(
                connection,
                "catalogue_snapshots",
                {
                    "id": f"legacy-{label}-snapshot",
                    "created_at": cutoff,
                    "source_id": "legacy-source",
                    "digest": frozen_digest({"policy": label, "revisions": revisions}),
                    "cutoff": cutoff,
                    "policy": "synthetic_demo",
                    "revision_ids": revisions,
                },
            )
        insert_row(
            connection,
            "feature_sets",
            {
                "id": "legacy-features",
                "created_at": day(3),
                "experiment_id": "legacy-experiment",
                "schema_version": "legacy-v1",
                "artifact": feature_ref,
                "partitions": {"fit": [day(0).isoformat(), day(3).isoformat()]},
            },
        )
        insert_row(
            connection,
            "model_versions",
            {
                "id": "legacy-model",
                "created_at": day(3),
                "experiment_id": "legacy-experiment",
                "algorithm": "historical_rate",
                "status": "trained",
                "training_cutoff": day(3),
                "available_at": day(3),
                "snapshot_id": "legacy-training-snapshot",
                "artifact": model_ref,
                "metadata": {"feature_schema": "legacy-v1", "seed": 2026, "nested": {"fitted": True}},
            },
        )
        # Original001 defers the child FK and seals the archive once the parent exists.
        insert_row(
            connection,
            "forecast_cells",
            {
                "forecast_id": "legacy-forecast",
                "cell_id": "legacy-cell",
                "probability": 0.123456789,
                "expected_count": 0.15,
                "band": "red",
            },
        )
        insert_row(
            connection,
            "forecast_runs",
            {
                "id": "legacy-forecast",
                "created_at": day(10) - timedelta(minutes=20),
                "logical_key": "legacy-publication",
                "experiment_id": "legacy-experiment",
                "model_id": "legacy-model",
                "snapshot_id": "legacy-training-snapshot",
                "issued_at": day(10) - timedelta(minutes=20),
                "start": day(10),
                "end": day(11),
                "input_cutoff": day(10) - timedelta(minutes=30),
                "mode": "synthetic_demo",
                "status": "published",
                "provenance": {"model_artifact": model_ref, "original_probabilities": True},
            },
        )
        insert_row(
            connection,
            "evaluation_cells",
            {
                "evaluation_id": "legacy-evaluation",
                "cell_id": "legacy-cell",
                "observed_count": 1,
                "outcome": "TP",
            },
        )
        insert_row(
            connection,
            "evaluation_runs",
            {
                "id": "legacy-evaluation",
                "created_at": day(13),
                "forecast_id": "legacy-forecast",
                "snapshot_id": "legacy-evaluation-snapshot",
                "version": 1,
                "status": "provisional",
                "reason": "Artificial complete interval",
                "metrics": {"positives": 1, "brier": 0.768328},
            },
        )
        insert_row(
            connection,
            "model_promotions",
            {
                "id": "legacy-promotion",
                "created_at": day(4),
                "experiment_id": "legacy-experiment",
                "model_id": "legacy-model",
                "reason": "Artificial archived promotion",
            },
        )
        insert_row(
            connection,
            "jobs",
            {
                "id": "legacy-job",
                "created_at": day(1),
                "logical_key": "legacy-job-key",
                "kind": "ping",
                "payload": {"legacy": True},
                "status": "running",
                "attempts": 1,
                "progress": 0.25,
                "heartbeat": day(1),
                "lease_until": day(1) + timedelta(minutes=2),
                "cancel_requested": False,
                "error": None,
                "result": {},
            },
        )
        insert_row(
            connection,
            "worker_state",
            {"id": "scheduler", "heartbeat": day(1), "details": {"active_job": "legacy-job"}},
        )
    return records, artifacts


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table: {
                tuple(row[key] for key in keys): row
                for row in connection.scalars(text(f"SELECT to_jsonb(t) FROM {table} t"))
            }
            for table, keys in TABLE_KEYS.items()
        }


def assert_preserved(engine, original, artifacts, artifact_dir, *, allow_appends=False):
    current = snapshot(engine)
    for table, rows in original.items():
        if not allow_appends:
            assert current[table].keys() == rows.keys(), f"Migration changed row identities in {table}"
        for identity, old in rows.items():
            actual = current[table][identity]
            for column, value in old.items():
                if allow_appends and table == "catalogue_sources" and column in {"details", "status"}:
                    continue  # Current ingestion legitimately advances mutable source health.
                assert actual[column] == value, f"Archived {table}.{column} changed for {identity}"
    for ref, content in artifacts.items():
        assert (artifact_dir / ref).read_bytes() == content, f"Archived bytes changed: {ref}"


def assert_refused(engine, statement, *, sqlstate="P0001"):
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    except DBAPIError as error:
        assert error.orig.sqlstate == sqlstate, f"Unexpected constraint failure: {error.orig.sqlstate}"
    else:
        raise AssertionError("Archive accepted a forbidden mutation")


def check_guards(engine, *, receipts=False):
    assert_refused(engine, "UPDATE earthquake_revisions SET magnitude=8 WHERE id='legacy-revision-0'")
    assert_refused(engine, "UPDATE forecast_cells SET probability=.9 WHERE forecast_id='legacy-forecast'")
    assert_refused(engine, "INSERT INTO forecast_cells VALUES ('legacy-forecast','extra',.1,NULL,'green')")
    assert_refused(engine, "INSERT INTO evaluation_cells VALUES ('legacy-evaluation','extra',0,'TN')")
    assert_refused(
        engine,
        "INSERT INTO earthquake_events VALUES ('missing-source','missing', 'invalid-event',now())",
        sqlstate="23503",
    )
    assert_refused(
        engine,
        """
        INSERT INTO earthquake_revisions SELECT
            event_id, origin_time, provider_updated, received_at, longitude, latitude, depth_km,
            magnitude, magnitude_type, status, repeat('f',64), raw_ref, location,
            'invalid-parent-revision', created_at, event_type, selection_reason, 'missing-parent'
        FROM earthquake_revisions WHERE id='legacy-revision-0'
    """,
        sqlstate="23503",
    )
    if receipts:
        assert_refused(engine, "UPDATE ingestion_runs SET status='failed' WHERE id='legacy-ingestion'")
        assert_refused(engine, "DELETE FROM ingestion_runs WHERE id='legacy-ingestion'")


def check_defaults(engine):
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT event_type, selection_reason, hash_parent_id FROM earthquake_revisions
            WHERE id IN ('legacy-revision-0','legacy-revision-1') ORDER BY id
        """)
        ).all()
    assert rows == [("earthquake", None, None), ("earthquake", None, None)]


def check_current_ingestion(engine, records):
    # Import evolving models only after the archive reaches the current schema.
    from yureatlas.catalogue import ensure_experiment, ingest_records, revision_content_hash, revisions_asof
    from yureatlas.db import engine as app_engine
    from yureatlas.schemas import ExperimentSpec

    try:
        assert ensure_experiment(ExperimentSpec(**CONFIG_002)).id == "legacy-experiment"
        duplicate = ingest_records("legacy-source", records, day(0), day(12), {}, received_at=day(20))
        assert (duplicate["added"], duplicate["duplicate"]) == (0, 2)
        for index, changes in ((0, {"magnitude": 2.8, "provider_updated": day(21)}), (1, {"magnitude": 2.8})):
            result = ingest_records(
                "legacy-source",
                [dict(records[index], **changes)],
                day(0),
                day(12),
                {},
                received_at=day(22 + index),
            )
            assert result["added"] == 1
        reverted = ingest_records("legacy-source", [records[1]], day(0), day(12), {}, received_at=day(24))
        assert reverted["added"] == 1
        repeated = ingest_records("legacy-source", [records[1]], day(0), day(12), {}, received_at=day(25))
        assert (repeated["added"], repeated["duplicate"]) == (0, 1)
        old = revisions_asof("legacy-source", day(13))
        assert {row.id for row in old} == {"legacy-revision-0", "legacy-revision-1"}
        current = revisions_asof("legacy-source", day(25))
        assert sorted(row.magnitude for row in current) == [2.8, 4.2]
        for row in [*old, *current]:
            assert row.content_hash == revision_content_hash(row, supersedes=row.hash_parent_id)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM earthquake_revisions")) == 5
            assert connection.scalar(text("SELECT count(*) FROM import_manifests")) == 0
    finally:
        app_engine.dispose()


def upgrade(target, env):
    validate_isolation(env)
    # Inherit the checker's process group so the outer harness can reap every child.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target], cwd=ROOT, env=env, timeout=120, check=True
    )


def main():
    env = dict(os.environ)
    validate_isolation(env)  # No application import, migration, artifact write or seed precedes ownership.
    artifact_dir = Path(env["ARTIFACT_DIR"])
    engine = create_engine(checked_url(env["DATABASE_URL"]), poolclass=NullPool, connect_args=CONNECT_ARGS)
    try:
        with engine.connect() as connection:
            if connection.scalar(text("SELECT to_regclass('public.alembic_version')")) is not None:
                raise ValueError("Migration acceptance requires a fresh harness-owned database")
        upgrade("002", env)
        validate_isolation(env)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "002"
        records, artifacts = seed_002(engine, artifact_dir)
        original = snapshot(engine)
        print(
            f"Seeded schema002: {sum(map(len, original.values()))} rows and {len(artifacts)} artifacts",
            flush=True,
        )
        for target in ("003", "004", "005", "006", "007", "008", "head"):
            upgrade(target, env)
            assert_preserved(engine, original, artifacts, artifact_dir)
            check_defaults(engine)
            if target in {"008", "head"}:
                with engine.connect() as connection:
                    assert connection.execute(
                        text("SELECT attempt_token, worker_token, execution FROM jobs WHERE id='legacy-job'")
                    ).one() == (None, None, None)
            validate_isolation(env)
            check_guards(engine, receipts=target != "003")
            print(
                f"Upgrade {target}: archived rows, hashes, probabilities, bytes and constraints preserved",
                flush=True,
            )
        validate_isolation(env)
        check_current_ingestion(engine, records)
        assert_preserved(engine, original, artifacts, artifact_dir, allow_appends=True)
        final = snapshot(engine)
        upgrade("head", env)
        assert snapshot(engine) == final, "Repeated upgrade changed current archive rows"
        assert_preserved(engine, original, artifacts, artifact_dir, allow_appends=True)
        print(
            "Current ingestion: legacy deduplication, corrections and receipt-time reversal passed; repeated upgrade unchanged",
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
