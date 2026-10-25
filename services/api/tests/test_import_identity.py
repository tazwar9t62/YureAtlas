"""Collection isolation and atomically enforced, immutable import identities."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, get_ident
from threading import Event as ThreadEvent
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from yureatlas import catalogue
from yureatlas.artifacts import digest, put
from yureatlas.catalogue import ensure_experiment, ingest_records, revisions_asof
from yureatlas.db import engine, transaction
from yureatlas.grid import StudyGrid
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Event, Experiment, ImportManifest, Ingestion, Revision, Source, now
from yureatlas.timeutil import utc

START = utc("2020-01-01T00:00:00Z")
END = START + timedelta(days=1)


def name():
    return "identity_" + uuid4().hex[:12]


def spec(**changes):
    return ExperimentSpec(
        **dict(
            {"name": name(), "provider": "jma", "west": 140, "east": 142, "south": 35, "north": 37},
            **changes,
        )
    )


@pytest.mark.parametrize("provider", ["usgs", "synthetic"])
def test_collection_namespace_is_only_valid_for_local_catalogues(provider):
    with pytest.raises(ValidationError, match="only supported for JMA"):
        spec(provider=provider, catalogue_namespace="official_2020")


@pytest.mark.parametrize(
    "namespace", ["ab", "a" * 81, "with space", "UPPER", "../path", "_leading", "-leading"]
)
def test_collection_namespace_has_an_explicit_bounded_identity(namespace):
    with pytest.raises(ValidationError):
        spec(catalogue_namespace=namespace)


def test_absent_optional_namespace_does_not_change_legacy_serialized_config():
    original = spec()
    config = original.model_dump(exclude_none=True)
    assert "catalogue_namespace" not in config
    assert digest(config) == digest(original.model_dump(exclude={"catalogue_namespace"}))
    assert spec(provider="usgs").model_dump(exclude_none=True)["provider"] == "usgs"


@pytest.fixture
def experiment():
    return ensure_experiment(spec(catalogue_namespace=name()))


def identity(experiment, raw=b"first line\n", **changes):
    format_name = changes.get("format", "jma")
    return dict(
        {
            "catalogue_namespace": experiment.config["catalogue_namespace"],
            "namespace": "file_2020",
            "format": format_name,
            "artifact": put(raw, "txt" if format_name == "jma" else "csv"),
            "line_count": len(raw.splitlines()),
            "mapping": {},
            "parser_version": format_name + "-v2",
            "coverage_declared": True,
        },
        **changes,
    )


def record(provider_id="file_2020:1", **changes):
    return dict(
        {
            "provider_id": provider_id,
            "origin_time": START,
            "provider_updated": None,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": None,
            "magnitude": 4.2,
            "magnitude_type": "J",
            "status": "reviewed",
        },
        **changes,
    )


def ingest(experiment, manifest, records=None, start=START, end=END):
    return ingest_records(
        experiment.source_id,
        [record()] if records is None else records,
        start,
        end,
        {"raw_ref": manifest["artifact"]},
        import_identity=manifest,
    )


def counts(source_id):
    with transaction() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table).where(table.source_id == source_id))
            for table in (ImportManifest, Ingestion, Event)
        ) + (
            session.scalar(
                select(func.count())
                .select_from(Revision)
                .join(Event, Event.id == Revision.event_id)
                .where(Event.source_id == source_id)
            ),
        )


@pytest.mark.integration
def test_namespace_source_isolation_and_legacy_reopen_compatibility():
    legacy_spec = spec()
    legacy = ensure_experiment(legacy_spec)
    assert ensure_experiment(legacy_spec).id == legacy.id
    assert "catalogue_namespace" not in legacy.config
    collection = name()
    first = ensure_experiment(spec(catalogue_namespace=collection))
    same = ensure_experiment(spec(catalogue_namespace=collection, target_magnitude=5))
    other = ensure_experiment(spec(catalogue_namespace=name()))
    wider = ensure_experiment(spec(catalogue_namespace=collection, east=143))
    assert first.source_id == same.source_id
    assert len({legacy.source_id, first.source_id, other.source_id, wider.source_id}) == 4
    with transaction() as session:
        source = session.get(Source, first.source_id)
        assert source.details["catalogue_namespace"] == collection
        assert session.get(Experiment, legacy.id).config_hash == digest(legacy.config)
    with pytest.raises(ValueError, match="explicit catalogue namespace"):
        ingest_records(legacy.source_id, [record()], START, END, {})
    with pytest.raises(ValueError, match="explicit JMA catalogue namespace"):
        ingest_records(legacy.source_id, [record()], START, END, {}, import_identity=identity(first))


@pytest.mark.integration
def test_identical_fixed_file_replay_returns_original_receipt_and_manifest_is_immutable(experiment):
    manifest = identity(experiment)
    first = ingest(experiment, manifest)
    second = ingest(experiment, manifest)
    assert second == dict(first, idempotent_replay=True)
    assert counts(experiment.source_id) == (1, 1, 1, 1)
    with transaction() as session:
        row = session.get(ImportManifest, first["import_manifest_id"])
        assert row.request_digest == first["request_digest"]
        assert row.artifact == manifest["artifact"]
        assert row.line_count == 1
        assert session.get(Ingestion, row.ingestion_id).report == first
    with pytest.raises(DBAPIError), transaction() as session:
        session.execute(text("UPDATE import_manifests SET namespace='changed' WHERE id=:id"), {"id": row.id})
    with pytest.raises(DBAPIError), transaction() as session:
        session.execute(text("DELETE FROM import_manifests WHERE id=:id"), {"id": row.id})
    with pytest.raises(DBAPIError), transaction() as session:
        session.execute(
            text("UPDATE ingestion_runs SET report='{}'::jsonb WHERE id=:id"), {"id": row.ingestion_id}
        )


@pytest.mark.integration
def test_unmanifested_legacy_receipts_remain_readable_and_are_immutable():
    legacy = ensure_experiment(spec(provider="synthetic"))
    report = ingest_records(legacy.source_id, [], START, END, {"legacy_evidence": True}, status="partial")
    with transaction() as session:
        receipt = session.scalar(select(Ingestion).where(Ingestion.source_id == legacy.source_id))
        assert receipt.report == report
        assert not session.scalar(
            select(ImportManifest.id).where(ImportManifest.source_id == legacy.source_id)
        )
    for mutation in (
        "UPDATE ingestion_runs SET status='complete' WHERE id=:id",
        "DELETE FROM ingestion_runs WHERE id=:id",
    ):
        with pytest.raises(DBAPIError), transaction() as session:
            session.execute(text(mutation), {"id": receipt.id})
    with transaction() as session:
        preserved = session.get(Ingestion, receipt.id)
        assert preserved.status == "partial"
        assert preserved.report == report


@pytest.mark.integration
@pytest.mark.parametrize("changed", [b"second\nfirst\n", b"first\nCHANGED\n", b"first\n"])
def test_fixed_namespace_refuses_reorder_changes_or_changed_line_count_atomically(experiment, changed):
    original = identity(experiment, b"first\nsecond\n")
    ingest(experiment, original)
    with pytest.raises(ValueError, match="namespace is immutable"):
        ingest(experiment, identity(experiment, changed), [record(magnitude=2.8)])
    assert counts(experiment.source_id) == (1, 1, 1, 1)
    assert revisions_asof(experiment.source_id, now())[0].magnitude == 4.2


@pytest.mark.integration
def test_distinct_fixed_files_cannot_overlap_or_reuse_content_but_adjacent_intervals_are_valid(experiment):
    ingest(experiment, identity(experiment, b"first\n"))
    for manifest, start, end in (
        (identity(experiment, b"different\n", namespace="file_b"), START, END),
        (identity(experiment, b"first\n", namespace="file_b"), END, END + timedelta(days=1)),
    ):
        with pytest.raises(ValueError, match="cannot overlap intervals or reuse"):
            ingest(experiment, manifest, [record("file_b:1")], start, end)
    second = identity(experiment, b"different\n", namespace="file_b")
    ingest(experiment, second, [record("file_b:1", origin_time=END)], END, END + timedelta(days=1))
    # A revised declaration for the same immutable file is a new manifest, if
    # it does not overlap another namespace. Existing events still deduplicate.
    ingest(experiment, identity(experiment, b"first\n"), start=START - timedelta(days=1), end=END)
    assert counts(experiment.source_id) == (3, 3, 2, 2)


@pytest.mark.integration
def test_csv_stable_ids_can_be_revised_but_collection_formats_cannot_mix(experiment):
    first = identity(experiment, b"id,mag\na,4.2\n", format="csv", mapping={"id": "provider_id"})
    ingest(experiment, first, [record("a")])
    changed = identity(experiment, b"id,mag\na,2.8\n", format="csv", mapping={"id": "provider_id"})
    ingest(experiment, changed, [record("a", magnitude=2.8)])
    assert revisions_asof(experiment.source_id, now())[0].magnitude == 2.8
    assert counts(experiment.source_id) == (2, 2, 1, 2)
    replay = ingest(experiment, first, [record("a")])
    assert replay["idempotent_replay"] is True
    assert revisions_asof(experiment.source_id, now())[0].magnitude == 2.8
    with pytest.raises(ValueError, match="cannot mix"):
        ingest(experiment, identity(experiment, b"fixed\n", namespace="fixed"))
    assert counts(experiment.source_id) == (2, 2, 1, 2)


@pytest.mark.integration
def test_identity_rejects_mismatched_source_line_count_and_untracked_legacy_history(experiment):
    for changes, message in (
        ({"catalogue_namespace": "another_collection"}, "explicit JMA catalogue namespace"),
        ({"line_count": 2}, "line count does not match"),
    ):
        with pytest.raises(ValueError, match=message):
            ingest(experiment, identity(experiment, **changes))
    assert counts(experiment.source_id) == (0, 0, 0, 0)
    with transaction() as session:
        session.add(
            Ingestion(source_id=experiment.source_id, start=START, end=END, status="partial", report={})
        )
    with pytest.raises(ValueError, match="cannot be adopted"):
        ingest(experiment, identity(experiment))
    assert counts(experiment.source_id) == (0, 1, 0, 0)


@pytest.mark.integration
def test_revision_failure_rolls_back_manifest_receipt_and_events(experiment, monkeypatch):
    manifest = identity(experiment)

    def fail_artifact(*_args):
        raise OSError("revision artifact unavailable")

    monkeypatch.setattr(catalogue, "put", fail_artifact)
    with pytest.raises(OSError, match="revision artifact unavailable"):
        ingest(experiment, manifest)
    assert counts(experiment.source_id) == (0, 0, 0, 0)


@pytest.mark.integration
def test_manifest_insert_failure_rolls_back_already_flushed_revisions_and_receipt(experiment):
    manifest = identity(experiment)

    def fail_manifest(*_args):
        raise RuntimeError("manifest insertion failed")

    event.listen(ImportManifest, "before_insert", fail_manifest)
    try:
        with pytest.raises(RuntimeError, match="manifest insertion failed"):
            ingest(experiment, manifest)
    finally:
        event.remove(ImportManifest, "before_insert", fail_manifest)
    assert counts(experiment.source_id) == (0, 0, 0, 0)
    with transaction() as session:
        assert session.get(Source, experiment.source_id).status == "uninitialized"


@pytest.mark.integration
def test_declared_interval_limits_discovery_but_preserves_known_moved_origin(experiment):
    ingest(experiment, identity(experiment, b"id,mag\nknown,4.2\n", format="csv"), [record("known")])
    moved = record("known", origin_time=END + timedelta(days=1))
    inside = record("inside", origin_time=START)
    records = [
        moved,
        inside,
        record("at_end", origin_time=END),
        record("before_start", origin_time=START - timedelta(seconds=1)),
    ]
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    selected = catalogue.discovery_and_corrections(
        experiment.source_id, records, grid, experiment.config, discovery_interval=(START, END)
    )
    assert selected == [moved, inside]


@pytest.mark.integration
def test_waiting_import_retains_correction_to_an_event_committed_by_the_lock_holder(experiment):
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    context = {"grid": grid, "config": experiment.config, "discovery_interval": (START, END)}
    original = identity(experiment, b"id,mag\nknown,4.2\n", format="csv")
    corrected = identity(experiment, b"id,mag\nknown,2.8\n", format="csv")
    first_inserted, second_waiting, release_first = ThreadEvent(), ThreadEvent(), ThreadEvent()
    threads = {}

    def hold_first_manifest(_mapper, _connection, target):
        if target.source_id == experiment.source_id and get_ident() == threads.get("first"):
            first_inserted.set()  # Event, revision and receipt were already flushed under the source lock.
            assert release_first.wait(timeout=15)

    def signal_second_lock(_connection, _cursor, statement, _parameters, _execution_context, _many):
        if get_ident() == threads.get("second") and "pg_advisory_xact_lock(hashtext" in statement:
            second_waiting.set()

    def deliver(label, manifest, magnitude):
        threads[label] = get_ident()
        return ingest_records(
            experiment.source_id,
            [record("known", magnitude=magnitude)],
            START,
            END,
            {"persisted_candidates": 999},  # Caller estimates must not override locked selection.
            import_identity=manifest,
            discovery_context=context,
        )

    event.listen(ImportManifest, "before_insert", hold_first_manifest)
    event.listen(engine, "before_cursor_execute", signal_second_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(deliver, "first", original, 4.2)
            try:
                assert first_inserted.wait(timeout=10)
                second = pool.submit(deliver, "second", corrected, 2.8)
                assert second_waiting.wait(timeout=10)
            finally:
                release_first.set()
            assert first.result(timeout=20)["persisted_candidates"] == 1
            result = second.result(timeout=20)
            assert result["added"] == result["persisted_candidates"] == 1
    finally:
        release_first.set()
        event.remove(ImportManifest, "before_insert", hold_first_manifest)
        event.remove(engine, "before_cursor_execute", signal_second_lock)
    assert counts(experiment.source_id) == (2, 2, 1, 2)
    rows = revisions_asof(experiment.source_id, now())
    assert rows[0].magnitude == 2.8
    assert len(catalogue.event_arrays(rows, grid, experiment.config)["t"]) == 0
    replay = deliver("replay", corrected, 2.8)
    assert replay["idempotent_replay"] is True
    assert counts(experiment.source_id) == (2, 2, 1, 2)
    assert revisions_asof(experiment.source_id, now())[0].id == rows[0].id


@pytest.mark.integration
def test_locked_discovery_reports_only_ids_actually_retained_when_requested(experiment):
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    context = {"grid": grid, "config": experiment.config, "capture_ids": True}
    result = ingest_records(
        experiment.source_id,
        [record("retained"), record("below", magnitude=2.8)],
        START,
        END,
        {},
        import_identity=identity(experiment, b"id,mag\nretained,4.2\nbelow,2.8\n", format="csv"),
        discovery_context=context,
    )
    assert result["persisted_candidates"] == 1
    assert result["persisted_provider_ids"] == ["retained"]
    assert context["capture_ids"] is True  # The shared caller context is not mutated.


@pytest.mark.integration
@pytest.mark.parametrize("conflict", ["changed_content", "overlap", "format"])
def test_concurrent_first_import_conflicts_are_serialized_atomically(experiment, conflict):
    first = identity(experiment, b"first\n")
    options = {
        "changed_content": {},
        "overlap": {"namespace": "file_b"},
        "format": {"format": "csv"},
    }[conflict]
    second = identity(experiment, b"second\n", **options)
    start = Barrier(2)

    def deliver(manifest):
        start.wait(timeout=5)
        try:
            ingest(experiment, manifest)
            return "accepted"
        except ValueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver, manifest) for manifest in (first, second)]
        assert sorted(future.result(timeout=20) for future in futures) == ["accepted", "refused"]
    assert counts(experiment.source_id) == (1, 1, 1, 1)
