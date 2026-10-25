"""Corrections must survive discovery filters and never revive older selections."""

from contextlib import contextmanager, nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from yureatlas import catalogue, ingestion, tasks
from yureatlas.artifacts import digest
from yureatlas.catalogue import (
    discovery_and_corrections,
    ensure_experiment,
    event_arrays,
    ingest_records,
    make_snapshot,
    revision_content_hash,
    revisions_asof,
)
from yureatlas.db import transaction
from yureatlas.eligibility import eligible_cell
from yureatlas.grid import StudyGrid
from yureatlas.providers import parse_csv, parse_jma
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Revision
from yureatlas.timeutil import utc

START = utc("2020-01-01T00:00:00Z")
CONFIG = {"provider": "jma", "input_magnitude": 3, "target_magnitude": 4}


def record(provider_id="known", **changes):
    return dict(
        {
            "provider_id": provider_id,
            "origin_time": START,
            "provider_updated": START,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": None,
            "magnitude": 4.2,
            "magnitude_type": "J",
            "status": "reviewed",
        },
        **changes,
    )


@pytest.fixture
def grid():
    return StudyGrid(west=140, east=142, south=35, north=37)


@pytest.mark.parametrize(
    "changes",
    [
        {"magnitude": 2.8},
        {"longitude": 150},
        {"event_type": "explosion"},
        {"selection_reason": "Not a JMA natural earthquake"},
        {"magnitude_type": "Mw"},
        {"magnitude_type": "JD"},
        {"status": "deleted"},
    ],
)
def test_same_policy_excludes_corrected_discoveries_and_model_inputs(grid, changes):
    original, corrected = record(), record(**changes)
    assert eligible_cell(original, grid, CONFIG) is not None
    assert eligible_cell(corrected, grid, CONFIG) is None
    events = event_arrays([SimpleNamespace(**corrected)], grid, CONFIG)
    assert len(events["t"]) == 0
    assert events["cell"].dtype.kind == "i"


def test_provider_policy_and_input_target_thresholds_are_distinct(grid):
    mixed = record(magnitude_type="mb", magnitude=3.5)
    usgs = dict(CONFIG, provider="usgs")
    assert eligible_cell(mixed, grid, usgs) is not None
    assert eligible_cell(mixed, grid, usgs, minimum=4) is None
    assert eligible_cell(mixed, grid, CONFIG) is None


@pytest.fixture
def known_lookup(monkeypatch):
    queries = []

    class Session:
        def scalars(self, query):
            params = query.compile().params
            assert params["source_id_1"] == "source"
            requested = params["provider_id_1"]
            queries.append(requested)
            return [name for name in requested if name == "known"]

    @contextmanager
    def fake_transaction():
        yield Session()

    monkeypatch.setattr(catalogue, "transaction", fake_transaction)
    return queries


def test_discovery_lookup_is_bounded_and_retains_only_known_corrections(grid, known_lookup):
    rows = [record(f"outside-{i}", magnitude=2.8) for i in range(2100)]
    correction = record("known", event_type="explosion")
    discovery = record("new")
    rows.extend([correction, discovery])
    assert discovery_and_corrections("source", rows, grid, CONFIG) == [correction, discovery]
    assert len(known_lookup) == 3
    assert max(map(len, known_lookup)) <= 1000


def test_discovery_and_correction_in_one_delivery_preserve_both_versions(grid, known_lookup):
    first = record("new")
    correction = record("new", magnitude=2.8, provider_updated=START + timedelta(hours=1))
    assert discovery_and_corrections("source", [first, correction], grid, CONFIG) == [first, correction]


def test_local_csv_import_preserves_all_candidates_until_locked_persistence(grid, known_lookup, monkeypatch):
    raw = (
        b"provider_id,origin_time,longitude,latitude,depth_km,magnitude,magnitude_type\n"
        b"known,2020-01-01T00:00:00Z,140.5,35.5,,2.8,J\n"
        b"foreign,2020-01-01T00:00:00Z,140.5,35.5,,2.8,J\n"
        b"new,2020-01-01T00:00:00Z,140.5,35.5,,4.2,J\n"
    )
    monkeypatch.setattr(
        tasks, "load_experiment", lambda _: (SimpleNamespace(source_id="source", config=CONFIG), grid)
    )
    monkeypatch.setattr("yureatlas.artifacts.read", lambda _: raw)
    persisted = []
    monkeypatch.setattr(tasks, "ingest_records", lambda source, rows, *a, **kw: persisted.extend(rows))
    tasks.execute(
        "import",
        {
            "experiment_id": "experiment",
            "artifact": "raw",
            "format": "csv",
            "namespace": "file",
            "start": START.isoformat(),
            "end": (START + timedelta(days=1)).isoformat(),
        },
        lambda _: None,
    )
    assert [r["provider_id"] for r in persisted] == ["known", "foreign", "new"]
    assert persisted[0]["magnitude"] == 2.8


def test_feed_keeps_known_reclassification_without_discovering_explosions(grid, known_lookup, monkeypatch):
    monkeypatch.setattr(ingestion, "usgs_attempt", lambda _: nullcontext(None))
    rows = [record("known", event_type="explosion"), record("foreign", event_type="explosion")]
    monkeypatch.setattr(
        ingestion,
        "load_experiment",
        lambda _: (SimpleNamespace(source_id="source", config=dict(CONFIG, provider="usgs")), grid),
    )
    monkeypatch.setattr(
        ingestion,
        "USGS",
        lambda **kw: SimpleNamespace(
            requests=[],
            feed=lambda: (rows, {"raw_ref": "raw", "received_at": START.isoformat(), "rejected_count": 0}),
            retrieve=lambda a, b, *args: iter(
                [(a, b, [], {"rejected_count": 0, "received_at": START.isoformat()})]
            ),
            retrieve_id=lambda identity: (rows[0], {"received_at": START.isoformat()}),
            close=lambda: None,
            budget_report=dict,
        ),
    )
    monkeypatch.setattr(ingestion, "known_ids_for_interval", lambda *a: ([], False))
    persisted = []

    def persist(source, records, *args, discovery_context=None, **kwargs):
        if discovery_context is not None:
            records = discovery_and_corrections(
                source, records, discovery_context["grid"], discovery_context["config"]
            )
        persisted.extend(records)
        return {
            "added": len(records),
            "duplicate": 0,
            "persisted_provider_ids": [r["provider_id"] for r in records],
        }

    monkeypatch.setattr(ingestion, "ingest_records", persist)
    ingestion.ingest_usgs("experiment", START, START + timedelta(days=1), use_feed=True)
    assert persisted == [rows[0], rows[0]]  # Feed evidence and authoritative ID correction.


@pytest.mark.parametrize("change", ["agency", "event_type", "magnitude_type", "hypocentre"])
def test_jma_retains_excluded_corrections_and_distinguishes_ambiguity(grid, change):
    original = Path(__file__).with_name("fixtures").joinpath("jma_2023_first.txt").read_bytes().strip()
    line = list(original.decode())
    if change == "agency":
        line[0] = "U"
    elif change == "event_type":
        line[60] = "3"
    elif change == "hypocentre":
        line[59] = "8"
    else:
        line[54] = "W"
    rows, report = parse_jma("".join(line).encode(), "stable")
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "stable:1"
    assert rows[0]["selection_reason"]
    assert report["rejected_count"] == int(change == "hypocentre")
    assert report["policy_excluded_count"] == int(change != "hypocentre")
    assert report["retained_corrections"] == 1
    assert eligible_cell(rows[0], grid, CONFIG, minimum=0) is None


def test_csv_optional_event_type_preserves_legacy_input_and_explicit_correction():
    raw = (
        b"provider_id,origin_time,longitude,latitude,depth_km,magnitude,magnitude_type,event_type\n"
        b"a,2020-01-01T00:00:00Z,140.5,35.5,,4.2,J,\n"
        b"b,2020-01-01T00:00:00Z,140.5,35.5,,4.2,J,explosion\n"
    )
    rows, report = parse_csv(raw)
    assert report["rejected_count"] == 0
    assert [r["event_type"] for r in rows] == ["earthquake", "explosion"]


def test_legacy_hash_recipe_is_preserved_but_reclassification_changes_identity():
    original = record()
    expected = digest(
        {
            k: original[k]
            for k in (
                "origin_time",
                "provider_updated",
                "longitude",
                "latitude",
                "depth_km",
                "magnitude",
                "magnitude_type",
                "status",
            )
        }
    )
    assert revision_content_hash(original) == expected
    assert revision_content_hash(dict(original, event_type="earthquake", selection_reason=None)) == expected
    assert revision_content_hash(dict(original, event_type="explosion")) != expected
    assert revision_content_hash(dict(original, selection_reason="Excluded agency")) != expected


@pytest.mark.parametrize("provider_updated", [None, START])
def test_conflicting_same_version_in_one_delivery_is_rejected_before_writes(monkeypatch, provider_updated):
    def forbidden_transaction():
        pytest.fail("Ambiguous delivery must be rejected before persistence")

    monkeypatch.setattr(catalogue, "transaction", forbidden_transaction)
    with pytest.raises(ValueError, match="Conflicting records"):
        ingest_records(
            "source",
            [
                record(provider_updated=provider_updated),
                record(provider_updated=provider_updated, magnitude=2.8),
            ],
            START,
            START + timedelta(days=1),
            {},
        )


@pytest.fixture
def experiment():
    return ensure_experiment(
        ExperimentSpec(
            name="selection_" + uuid4().hex[:12], provider="synthetic", west=140, east=142, south=35, north=37
        )
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "changes",
    [
        {"magnitude": 2.8},
        {"longitude": 150},
        {"event_type": "explosion"},
        {"selection_reason": "Excluded agency"},
        {"origin_time": START + timedelta(days=10)},
    ],
)
def test_authoritative_revision_is_selected_before_experiment_filters(experiment, grid, changes):
    original = record(experiment.name)
    cutoff = START + timedelta(days=1)
    ingest_records(experiment.source_id, [original], START, cutoff, {}, received_at=cutoff)
    snapshot, old_rows = make_snapshot(experiment.source_id, cutoff, "synthetic_demo")
    correction = dict(original, provider_updated=cutoff, **changes)
    assert discovery_and_corrections(experiment.source_id, [correction], grid, experiment.config) == [
        correction
    ]
    ingest_records(
        experiment.source_id, [correction], START, cutoff, {}, received_at=cutoff + timedelta(days=1)
    )
    assert [r.id for r in revisions_asof(experiment.source_id, cutoff)] == snapshot.revision_ids
    rows = revisions_asof(experiment.source_id, cutoff + timedelta(days=1), origin_before=cutoff)
    assert len(event_arrays(rows, grid, experiment.config)["t"]) == 0
    assert len(event_arrays(old_rows, grid, experiment.config)["t"]) == 1
    with transaction() as s:
        assert s.scalar(
            select(Revision).where(Revision.id == old_rows[0].id)
        ).content_hash == revision_content_hash(original)


@pytest.mark.integration
def test_receipt_ordered_return_to_old_content_appends_then_redelivery_deduplicates(experiment):
    original = record(experiment.name, provider_updated=None)
    first = START + timedelta(days=1)
    snapshots = []
    for index, changes in enumerate(({}, {"magnitude": 2.8}, {}, {})):
        receipt = first + timedelta(days=index)
        report = ingest_records(
            experiment.source_id, [dict(original, **changes)], START, first, {}, received_at=receipt
        )
        assert (report["added"], report["duplicate"]) == ((0, 1) if index == 3 else (1, 0))
        snapshots.append(revisions_asof(experiment.source_id, receipt)[0])
    assert len({r.id for r in snapshots[:3]}) == 3
    assert snapshots[0].magnitude == snapshots[2].magnitude == 4.2
    assert snapshots[1].magnitude == 2.8
    assert snapshots[2].id == snapshots[3].id
    assert snapshots[0].content_hash == revision_content_hash(original)
    assert snapshots[2].content_hash != snapshots[0].content_hash
    assert snapshots[0].hash_parent_id is None
    assert snapshots[1].hash_parent_id == snapshots[0].id
    assert snapshots[2].hash_parent_id == snapshots[1].id
    for row in snapshots:
        assert row.content_hash == revision_content_hash(row, supersedes=row.hash_parent_id)
    assert revisions_asof(experiment.source_id, first)[0].id == snapshots[0].id


@pytest.mark.integration
def test_legacy_style_redelivery_with_explicit_defaults_remains_duplicate(experiment):
    original = record(experiment.name)
    receipt = START + timedelta(days=1)
    ingest_records(experiment.source_id, [original], START, receipt, {}, received_at=receipt)
    old = revisions_asof(experiment.source_id, receipt)[0]
    report = ingest_records(
        experiment.source_id,
        [dict(original, event_type="earthquake", selection_reason=None)],
        START,
        receipt,
        {},
        received_at=receipt + timedelta(days=1),
    )
    assert (report["added"], report["duplicate"]) == (0, 1)
    assert revisions_asof(experiment.source_id, receipt + timedelta(days=1))[0].id == old.id
