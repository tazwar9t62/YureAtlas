"""Publication boundaries in a harness-owned database and artifact directory."""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from threading import Event as ThreadEvent
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from yureatlas import archive, learning
from yureatlas.artifacts import digest, put, read
from yureatlas.catalogue import ensure_experiment, ingest_records, load_experiment, make_snapshot
from yureatlas.config import settings
from yureatlas.db import Session, engine, transaction
from yureatlas.rate import HistoricalRate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Event, Forecast, ForecastCell, Revision, Snapshot, Source
from yureatlas.timeutil import JST

pytestmark = pytest.mark.integration
MICROSECOND = timedelta(microseconds=1)


def event_record(identity, origin, **changes):
    return {
        "provider_id": identity,
        "origin_time": origin,
        "provider_updated": origin,
        "longitude": 140.5,
        "latitude": 35.5,
        "depth_km": 12,
        "magnitude": 4.2,
        "magnitude_type": "J",
        "status": "reviewed",
        "event_type": "earthquake",
        **changes,
    }


@pytest.fixture
def publication(monkeypatch):
    identity = "prospective_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="jma",
            catalogue_namespace=identity,
            west=140,
            east=141,
            south=35,
            north=36,
        )
    )
    _, grid = load_experiment(experiment.id)
    with transaction() as session:
        wall = session.scalar(select(func.clock_timestamp()))
    clock = SimpleNamespace(value=wall)
    monkeypatch.setattr(archive, "now", lambda: clock.value)
    cutoff = wall - timedelta(minutes=15)
    training_cutoff = wall - timedelta(days=2)
    snapshot, _ = make_snapshot(experiment.source_id, training_cutoff, "retrospective_revised")
    artifact = HistoricalRate(np.full(len(grid.ids), 0.02), 1.4, 0.05, 5)
    # The artifact is unique to this test, so deliberate checksum failures cannot
    # damage another fixture's content-addressed file.
    artifact.fixture_identity = identity
    clock.value = wall - timedelta(hours=1)
    model = archive.save_model(
        experiment, "historical_rate", artifact, training_cutoff, snapshot.id, {"feature_schema": "rate-v1"}
    )
    clock.value = wall
    assert read(model.artifact)
    np.testing.assert_array_equal(archive.load_model(model).predict(), artifact.predict())
    return SimpleNamespace(
        experiment=experiment,
        grid=grid,
        model=model,
        artifact=artifact,
        clock=clock,
        wall=wall,
        start=cutoff + timedelta(minutes=30),
        cutoff=cutoff,
    )


def cover(case, *, receipt=None, start=None, end=None, status="complete", report=None):
    return ingest_records(
        case.experiment.source_id,
        [],
        start if start is not None else case.cutoff - timedelta(days=1),
        end if end is not None else case.cutoff - timedelta(minutes=30),
        report or {},
        received_at=receipt if receipt is not None else case.cutoff,
        status=status,
        import_identity=import_identity(case, []),
    )


def deliver(case, records, receipt):
    return ingest_records(
        case.experiment.source_id,
        records,
        min(row["origin_time"] for row in records),
        max(row["origin_time"] for row in records) + MICROSECOND,
        {"coverage_effect": "supplement"},
        received_at=receipt,
        status="partial",
        import_identity=import_identity(case, records),
    )


def import_identity(case, records):
    buffer = io.StringIO(newline="")
    columns = list(event_record("", case.cutoff))
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(records)
    raw = buffer.getvalue().encode()
    return {
        "catalogue_namespace": case.experiment.config["catalogue_namespace"],
        "namespace": "receipt_" + uuid4().hex,
        "format": "csv",
        "artifact": put(raw, "csv"),
        "line_count": len(raw.splitlines()),
        "mapping": {},
        "parser_version": "csv-v2",
        "coverage_declared": True,
    }


def issue(case, **kwargs):
    return archive.issue_forecast(
        case.experiment.id, case.model.id, kwargs.pop("start", case.start), **kwargs
    )


def assert_no_publication(case):
    with transaction() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Forecast).where(Forecast.experiment_id == case.experiment.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ForecastCell)
                .outerjoin(Forecast, Forecast.id == ForecastCell.forecast_id)
                .where(Forecast.id.is_(None))
            )
            == 0
        )


def assert_complete(case, forecast):
    with transaction() as session:
        runs = list(session.scalars(select(Forecast).where(Forecast.experiment_id == case.experiment.id)))
        cells = list(session.scalars(select(ForecastCell).where(ForecastCell.forecast_id == forecast.id)))
    assert [row.id for row in runs] == [forecast.id]
    assert {cell.cell_id for cell in cells} == set(case.grid.ids)
    assert len(cells) == len(case.grid.ids)
    np.testing.assert_allclose([cell.probability for cell in cells], case.artifact.predict())


def test_receipt_cutoff_and_origin_boundary_preserve_authoritative_input_evidence(publication, monkeypatch):
    case = publication
    old = event_record("corrected", case.cutoff - timedelta(hours=2), magnitude=4.8)
    deliver(case, [old], case.cutoff - timedelta(minutes=2))
    corrected = dict(old, magnitude=2.5, provider_updated=case.cutoff - timedelta(minutes=1))
    exact = event_record("exact-receipt", case.cutoff - MICROSECOND)
    origin_boundary = event_record("exact-origin", case.cutoff, magnitude=5.3)
    deliver(case, [corrected, exact, origin_boundary], case.cutoff)
    deliver(
        case,
        [event_record("late-receipt", case.cutoff - timedelta(hours=1), magnitude=5.2)],
        case.cutoff + MICROSECOND,
    )
    cover(case)  # Coverage received exactly at cutoff is available.
    # A later partial receipt cannot rewrite what coverage was known at cutoff.
    cover(case, receipt=case.cutoff + MICROSECOND, status="partial")
    captured = []
    original = archive.event_arrays

    def capture(*args):
        arrays = original(*args)
        captured.append(arrays)
        return arrays

    monkeypatch.setattr(archive, "event_arrays", capture)
    forecast = issue(case)
    with transaction() as session:
        snapshot = session.get(Snapshot, forecast.snapshot_id)
        rows = session.execute(
            select(Event.provider_id, Revision.magnitude)
            .join(Revision, Revision.event_id == Event.id)
            .where(Revision.id.in_(snapshot.revision_ids))
        ).all()
    assert snapshot.cutoff == case.cutoff and snapshot.policy == "prospective"
    assert dict(rows) == {"corrected": 2.5, "exact-receipt": 4.2}
    np.testing.assert_array_equal(captured[0]["m"], [4.2])
    assert_complete(case, forecast)


@pytest.mark.parametrize("evidence", ["late_receipt", "supplement", "new_partial", "one_microsecond_gap"])
def test_inadequate_authoritative_coverage_withholds_publication(publication, evidence):
    case = publication
    if evidence == "late_receipt":
        cover(case, receipt=case.cutoff + MICROSECOND)
    elif evidence == "supplement":
        cover(case, report={"coverage_effect": "supplement"})
    elif evidence == "new_partial":
        cover(case, receipt=case.cutoff - MICROSECOND)
        cover(case, status="partial")
    else:
        cover(case, end=case.cutoff - timedelta(minutes=30) - MICROSECOND)
    with pytest.raises(ValueError, match="Recent input coverage unavailable"):
        issue(case)
    assert_no_publication(case)


def test_failed_source_with_complete_coverage_is_withheld(publication):
    cover(publication)
    with transaction() as session:
        session.get(Source, publication.experiment.source_id).status = "failed"
    with pytest.raises(ValueError, match="Recent input coverage unavailable"):
        issue(publication)
    assert_no_publication(publication)


@pytest.mark.parametrize("invalid", ["unavailable", "foreign_experiment"])
def test_unavailable_or_foreign_models_cannot_publish(publication, invalid):
    case = publication
    cover(case)
    if invalid == "unavailable":
        case.model = archive.save_model(
            case.experiment,
            "historical_rate",
            None,
            case.model.training_cutoff,
            case.model.snapshot_id,
            {"reason": "Deliberately unavailable fixture"},
            status="insufficient_data",
        )
        with pytest.raises(ValueError, match="Model unavailable: Deliberately unavailable fixture"):
            issue(case)
    else:
        identity = "foreign_" + uuid4().hex[:12]
        foreign = ensure_experiment(
            ExperimentSpec(**{**case.experiment.config, "name": identity, "catalogue_namespace": identity})
        )
        with pytest.raises(ValueError, match="Model and experiment are incompatible"):
            archive.issue_forecast(foreign.id, case.model.id, case.start)
        with transaction() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(Forecast).where(Forecast.experiment_id == foreign.id)
                )
                == 0
            )
    assert_no_publication(case)


@pytest.mark.parametrize(
    "boundary, message",
    [
        ("future_cutoff", "Future input cutoff"),
        ("late_start", "Missed publication deadline before computation"),
        ("future_model", "Model artifact unavailable at issuance"),
        ("training_cutoff", "Training and feature cutoffs"),
        ("wrong_cutoff", "input cutoff exactly 30 minutes"),
        ("naive_start", "Timestamp must include a timezone"),
        ("naive_cutoff", "Timestamp must include a timezone"),
    ],
)
def test_initial_time_guards_reach_the_intended_real_source_path(publication, boundary, message):
    case = publication
    cover(case)
    kwargs = {}
    if boundary == "future_cutoff":
        case.clock.value = case.cutoff - MICROSECOND
    elif boundary == "late_start":
        case.clock.value = case.start
    elif boundary in {"future_model", "training_cutoff"}:
        case.clock.value = case.wall + MICROSECOND if boundary == "future_model" else case.wall
        case.model = archive.save_model(
            case.experiment,
            "historical_rate",
            case.artifact,
            case.cutoff + MICROSECOND if boundary == "training_cutoff" else case.model.training_cutoff,
            case.model.snapshot_id,
            {},
        )
        case.clock.value = case.wall
    elif boundary == "wrong_cutoff":
        kwargs["cutoff"] = case.cutoff + MICROSECOND
    elif boundary == "naive_start":
        kwargs["start"] = case.start.replace(tzinfo=None)
    else:
        kwargs["cutoff"] = case.cutoff.replace(tzinfo=None)
    with pytest.raises(ValueError, match=message):
        issue(case, **kwargs)
    assert_no_publication(case)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unavailable_artifact_never_publishes_cells(publication, damage):
    case = publication
    cover(case)
    path = settings.artifact_dir / case.model.artifact
    original = path.read_bytes()
    try:
        if damage == "missing":
            path.unlink()
            error, message = FileNotFoundError, None
        else:
            path.write_bytes(b"deliberately corrupted owned test artifact")
            error, message = ValueError, "Artifact checksum mismatch"
        with pytest.raises(error, match=message):
            issue(case)
        assert_no_publication(case)
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("stage", ["inference", "provenance", "flush"])
def test_deadline_crossed_during_work_rolls_back_publication(publication, monkeypatch, stage):
    case = publication
    cover(case)
    reached = []
    if stage == "inference":
        original = HistoricalRate.predict

        def predict(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            reached.append(stage)
            case.clock.value = case.start
            return result

        monkeypatch.setattr(HistoricalRate, "predict", predict)
    elif stage == "provenance":
        original = archive.code_provenance

        def provenance():
            result = original()
            reached.append(stage)
            case.clock.value = case.start
            return result

        monkeypatch.setattr(archive, "code_provenance", provenance)
    else:

        def after_flush(session, _context):
            if any(
                isinstance(row, Forecast) and row.experiment_id == case.experiment.id for row in session.new
            ):
                reached.append(stage)
                case.clock.value = case.start

        sqlalchemy_event.listen(Session.class_, "after_flush", after_flush)
    try:
        with pytest.raises(ValueError, match="Missed publication deadline"):
            issue(case)
    finally:
        if stage == "flush":
            sqlalchemy_event.remove(Session.class_, "after_flush", after_flush)
    assert reached == [stage]
    assert_no_publication(case)


def test_deadline_is_rechecked_after_waiting_for_the_publication_lock(publication):
    case = publication
    cover(case)
    key = digest(
        {"experiment": case.experiment.id, "model": case.model.id, "start": case.start, "mode": "prospective"}
    )
    reached = ThreadEvent()

    def before_execute(_connection, _cursor, statement, parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement and key in parameters.values():
            reached.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction() as holder:
            holder.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
            sqlalchemy_event.listen(engine, "before_cursor_execute", before_execute)
            try:
                future = pool.submit(issue, case)
                assert reached.wait(10), "Publisher did not reach its logical-key lock"
                case.clock.value = case.start
            finally:
                sqlalchemy_event.remove(engine, "before_cursor_execute", before_execute)
        with pytest.raises(ValueError, match="Missed publication deadline during computation or publication"):
            future.result(timeout=10)
    assert_no_publication(case)


def test_database_wall_clock_rejects_late_application_clock_at_transaction_finalization(publication):
    case = publication
    case.start = case.wall - timedelta(seconds=1)
    case.cutoff = case.start - timedelta(minutes=30)
    case.clock.value = case.start - timedelta(minutes=1)
    cover(case)
    flushed = []

    def after_flush(session, _context):
        if any(isinstance(row, Forecast) and row.experiment_id == case.experiment.id for row in session.new):
            flushed.append(True)

    sqlalchemy_event.listen(Session.class_, "after_flush", after_flush)
    try:
        with pytest.raises(
            ValueError, match="Missed publication deadline during transaction finalization"
        ) as error:
            issue(case)
    finally:
        sqlalchemy_event.remove(Session.class_, "after_flush", after_flush)
    assert flushed == [True], "The real DB failure must occur after the forecast has flushed"
    assert isinstance(error.value.__cause__, DBAPIError)
    assert error.value.__cause__.orig.sqlstate == "23514"
    assert error.value.__cause__.orig.diag.constraint_name == "prospective_publication_deadline"
    assert_no_publication(case)


def insert_forecast(session, case, *, start, issued, identity=None, logical_key=None, created_at=None):
    identity = identity or uuid4().hex
    session.execute(
        insert(ForecastCell),
        [
            {"forecast_id": identity, "cell_id": cell, "probability": float(p), "band": "yellow"}
            for cell, p in zip(case.grid.ids, case.artifact.predict())
        ],
    )
    values = {
        "id": identity,
        "logical_key": logical_key or uuid4().hex,
        "experiment_id": case.experiment.id,
        "model_id": case.model.id,
        "snapshot_id": case.model.snapshot_id,
        "issued_at": issued,
        "start": start,
        "end": start + timedelta(days=1),
        "input_cutoff": start - timedelta(minutes=30),
        "mode": "prospective",
        "provenance": {"grid_hash": case.grid.hash},
    }
    if created_at is not None:
        values["created_at"] = created_at
    row = Forecast(**values)
    session.add(row)
    session.flush()
    return row


def test_deferred_guard_also_rejects_direct_sql_without_leaving_partial_cells(publication):
    case = publication
    with pytest.raises(DBAPIError) as error, transaction() as session:
        start = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        row = insert_forecast(session, case, start=start, issued=start - timedelta(minutes=1))
        assert session.get(Forecast, row.id) is row  # BEFORE checks and flush have succeeded.
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "prospective_publication_deadline"
    assert_no_publication(case)


def test_deferred_guard_uses_advancing_database_clock_after_predeadline_flush(publication):
    case = publication
    with pytest.raises(DBAPIError) as error, transaction() as session:
        session.execute(text("SET LOCAL statement_timeout='4000ms'"))
        start = session.scalar(select(func.clock_timestamp())) + timedelta(seconds=2)
        frozen_transaction_time = session.scalar(select(func.current_timestamp()))
        row = insert_forecast(session, case, start=start, issued=start - timedelta(minutes=1))
        assert session.get(Forecast, row.id) is row
        assert frozen_transaction_time < session.scalar(select(func.clock_timestamp())) < start
        # Only this acceptance case waits for wall time. The wait is bounded by
        # the two-second lead plus50ms and a PostgreSQL statement timeout.
        session.execute(
            text("SELECT pg_sleep(greatest(0, extract(epoch FROM (:target - clock_timestamp()))) + 0.05)"),
            {"target": start},
        )
        assert session.scalar(select(func.clock_timestamp())) >= start
        assert session.scalar(select(func.current_timestamp())) == frozen_transaction_time
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "prospective_publication_deadline"
    assert_no_publication(case)


def test_successful_retry_is_exact_and_normalizes_equivalent_timezone_instants(publication):
    case = publication
    cover(case)
    forecast = issue(case)
    case.clock.value = case.start + timedelta(days=1)
    retry = issue(case, start=case.start.astimezone(JST), cutoff=case.cutoff.astimezone(JST))
    assert retry.id == forecast.id and retry.logical_key == forecast.logical_key
    assert retry.issued_at == forecast.issued_at == case.wall
    assert retry.input_cutoff == case.cutoff
    assert_complete(case, forecast)


def test_concurrent_retries_publish_one_complete_forecast(publication, monkeypatch):
    case = publication
    cover(case)
    ready = Barrier(2)
    original = archive.load_model

    def load(model):
        artifact = original(model)
        ready.wait(timeout=10)
        return artifact

    monkeypatch.setattr(archive, "load_model", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(issue, case) for _ in range(2)]
        forecasts = [future.result(timeout=15) for future in futures]
    assert forecasts[0].id == forecasts[1].id
    assert_complete(case, forecasts[0])


def test_legacy_offset_keys_are_reused_without_rewriting_existing_archive(publication):
    case = publication
    offset_key = digest(
        {
            "experiment": case.experiment.id,
            "model": case.model.id,
            "start": case.start.astimezone(JST),
            "mode": "prospective",
        }
    )
    with transaction() as session:
        original = insert_forecast(
            session,
            case,
            start=case.start,
            issued=case.wall,
            logical_key=offset_key,
            created_at=case.wall - timedelta(seconds=1),
        )
        newer = insert_forecast(session, case, start=case.start, issued=case.wall, created_at=case.wall)
    case.clock.value = case.start + timedelta(days=1)
    retry = issue(case)
    assert retry.id == original.id and retry.logical_key == offset_key
    with transaction() as session:
        assert set(
            session.scalars(select(Forecast.id).where(Forecast.experiment_id == case.experiment.id))
        ) == {
            original.id,
            newer.id,
        }


@pytest.mark.parametrize(
    "invalid", ["probability_shape", "probability_nan", "expected_negative", "expected_shape"]
)
def test_invalid_prediction_output_cannot_publish_partial_cells(publication, monkeypatch, invalid):
    case = publication
    cover(case)
    case.model = archive.save_model(
        case.experiment,
        "fixture_predictor",
        {"fixture": case.experiment.name},
        case.model.training_cutoff,
        case.model.snapshot_id,
        {},
    )
    probabilities = np.full(len(case.grid.ids), 0.1)
    expected = np.full(len(case.grid.ids), 0.2)
    if invalid == "probability_shape":
        probabilities = probabilities[:, None]
    elif invalid == "probability_nan":
        probabilities[0] = np.nan
    elif invalid == "expected_negative":
        expected[0] = -0.1
    else:
        expected = expected[:-1]
    monkeypatch.setattr(learning, "predict_artifact", lambda *args: (probabilities, expected))
    with pytest.raises(ValueError, match="Invalid or incomplete model output"):
        issue(case)
    assert_no_publication(case)


def test_unrelated_database_constraint_failure_is_not_reported_as_a_deadline(publication):
    case = publication
    cover(case)
    cause = RuntimeError("An unrelated constraint failed")
    cause.sqlstate = "23514"
    cause.diag = SimpleNamespace(constraint_name="unrelated_constraint")
    failure = DBAPIError("INSERT", {}, cause)

    def before_execute(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO forecast_cells"):
            raise failure

    sqlalchemy_event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with pytest.raises(DBAPIError) as error:
            issue(case)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", before_execute)
    assert error.value is failure
    assert_no_publication(case)
