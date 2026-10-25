"""Pinned targets, deterministic resolution and owned legacy-job recovery."""

import io
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import joblib
import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as OrmSession
from yureatlas import archive, forecast_execution, jobs
from yureatlas.artifacts import canonical, put
from yureatlas.catalogue import ensure_experiment, ingest_records, load_experiment
from yureatlas.db import Session, transaction
from yureatlas.execution import JobOwnershipLost, execution_scope
from yureatlas.forecast_errors import LegacyRetryIdentityUnknown, PublicationDeadlineMissed
from yureatlas.forecast_execution_schema import ForecastTarget, ResolvedForecastExecution
from yureatlas.rate import HistoricalRate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Forecast, ForecastCell, Job, Model, Promotion
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session

REQUEST = utc("2025-01-01T23:50:00+09:00")


def test_manual_target_is_request_time_plus_thirty_minutes_without_mutating_payload():
    payload = {"experiment_id": "requested-experiment", "model_id": "requested-model"}
    original = deepcopy(payload)
    pin = forecast_execution.pin_forecast_target(payload, REQUEST)
    assert pin["version"] == 1 and pin["state"] == "target_pinned"
    assert pin["mode"] == "prospective" and pin["target_basis"] == "request_time"
    assert utc(pin["request_time"]) == utc(pin["input_cutoff"]) == REQUEST
    assert utc(pin["start"]) == REQUEST + timedelta(minutes=30)
    assert utc(pin["end"]) == REQUEST + timedelta(days=1, minutes=30)
    assert payload == original
    assert "experiment_id" not in pin and "model_id" not in pin


def test_explicit_target_has_identical_identity_for_equivalent_aware_offsets():
    payloads = [
        {"start": "2025-01-02T00:00:00+09:00", "scheduled": True},
        {"start": "2025-01-01T15:00:00Z", "scheduled": True},
        {"start": "2025-01-01T07:00:00-08:00", "scheduled": True},
    ]
    pins = [forecast_execution.pin_forecast_target(payload, REQUEST) for payload in payloads]
    assert pins == [pins[0]] * 3
    assert pins[0]["target_basis"] == "explicit_start"
    assert utc(pins[0]["input_cutoff"]) == utc("2025-01-01T14:30:00Z")


@pytest.mark.parametrize("field", ["request_time", "start"])
def test_target_pin_rejects_naive_datetimes(field):
    payload, requested = {"start": REQUEST + timedelta(minutes=30)}, REQUEST
    if field == "request_time":
        requested = requested.replace(tzinfo=None)
    else:
        payload["start"] = payload["start"].replace(tzinfo=None)
    with pytest.raises(ValueError):
        forecast_execution.pin_forecast_target(payload, requested)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"state": "unknown"},
        {"mode": "synthetic_demo"},
        {"target_basis": "current_clock_on_retry"},
        {"start": "2025-01-01T15:20:00"},
        {"end": "2025-01-02T03:20:00Z"},
        {"input_cutoff": "2025-01-01T15:15:00Z"},
    ],
)
def test_stored_target_schema_rejects_invalid_chronology_or_policy(changes):
    pin = forecast_execution.pin_forecast_target({}, REQUEST)
    with pytest.raises(ValidationError):
        ForecastTarget.model_validate(dict(pin, **changes))


def test_target_only_record_is_not_a_valid_resolved_execution():
    pin = forecast_execution.pin_forecast_target({}, REQUEST)
    with pytest.raises(ValidationError):
        ResolvedForecastExecution.model_validate(dict(pin, state="resolved"))


@pytest.fixture
def owned_jobs():
    identities = set()

    def restrict(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id.in_(identities))

    def create(payload, created_at, *, legacy=False, attempts=0):
        row = Job(
            kind="forecast",
            logical_key="forecast-resolution:" + uuid4().hex,
            created_at=created_at,
            payload=deepcopy(payload),
            attempts=attempts,
            execution=None if legacy else forecast_execution.pin_forecast_target(payload, created_at),
        )
        with transaction() as session:
            session.add(row)
            session.flush()
            identities.add(row.id)
        return row

    event.listen(OrmSession, "do_orm_execute", restrict)
    try:
        yield create
    finally:
        event.remove(OrmSession, "do_orm_execute", restrict)
        with Session.begin() as session:
            session.execute(
                update(Job)
                .where(Job.id.in_(identities), Job.status.in_(["queued", "running"]))
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )


@pytest.fixture
def catalogue():
    identity = "forecast_resolution_" + uuid4().hex[:12]
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
        wall = session.scalar(text("SELECT clock_timestamp()"))
    start = wall + timedelta(minutes=15)
    cutoff = start - timedelta(minutes=30)
    artifact = HistoricalRate(np.full(len(grid.ids), 0.02), 1.4, 0.05, 30)
    artifact.fixture_identity = identity
    buffer = io.BytesIO()
    joblib.dump(artifact, buffer, compress=3)
    reference = put(buffer.getvalue(), "joblib")

    def model(*, identity=None, status="available", algorithm="historical_rate", experiment_id=None):
        row = Model(
            id=identity or str(uuid4()),
            created_at=wall - timedelta(hours=1),
            experiment_id=experiment_id or experiment.id,
            algorithm=algorithm,
            status=status,
            training_cutoff=wall - timedelta(days=2),
            available_at=wall - timedelta(hours=1),
            artifact=reference if status == "available" else None,
            metadata_={"feature_schema": "rate-v1", "fixture": identity},
        )
        with transaction() as session:
            session.add(row)
            session.flush()
        return row

    def promote(model_id, *, identity=None):
        row = Promotion(
            id=identity or str(uuid4()),
            created_at=wall - timedelta(minutes=30),
            experiment_id=experiment.id,
            model_id=model_id,
            reason="Resolver acceptance fixture",
        )
        with transaction() as session:
            session.add(row)
            session.flush()
        return row

    return SimpleNamespace(
        experiment=experiment,
        wall=wall,
        start=start,
        cutoff=cutoff,
        model=model,
        promote=promote,
        artifact=reference,
    )


def stored_job(identity):
    with transaction() as session:
        return session.get(Job, identity)


def resolve(owner, attempt, payload):
    with execution_scope(owner, job_id=attempt.id, attempt_token=attempt.attempt_token):
        return forecast_execution.resolve_forecast(payload)


def cover_for_publication(case):
    raw = b"provider_id,origin_time,provider_updated,longitude,latitude,depth_km,magnitude,magnitude_type,status,event_type\n"
    return ingest_records(
        case.experiment.source_id,
        [],
        case.cutoff - timedelta(days=1),
        case.cutoff - timedelta(minutes=30),
        {},
        received_at=case.cutoff,
        import_identity={
            "catalogue_namespace": case.experiment.config["catalogue_namespace"],
            "namespace": "coverage",
            "format": "csv",
            "artifact": put(raw, "csv"),
            "line_count": 1,
            "mapping": {},
            "parser_version": "csv-v2",
            "coverage_declared": True,
        },
    )


@pytest.mark.integration
@pytest.mark.parametrize("selection", ["historical_rate", "promotion"])
def test_equal_archive_times_use_descending_ids_for_model_and_promotion_selection(
    catalogue, owned_jobs, selection
):
    case = catalogue
    suffix = uuid4().hex
    high = case.model(identity="f" + suffix)
    low = case.model(identity="0" + suffix)  # Insert order deliberately differs from ID order.
    expected, promotion = high, None
    if selection == "promotion":
        promotion = case.promote(low.id, identity="f" + suffix)
        case.promote(high.id, identity="0" + suffix)
        expected = low  # Promotion ordering takes precedence over model ordering.
    payload = {"experiment_id": case.experiment.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    original_payload, original_pin = canonical(job.payload), deepcopy(job.execution)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        result = resolve(owner, attempt, payload)
        assert resolve(owner, attempt, payload) == result
    assert result["model_id"] == expected.id and result["selection"]["kind"] == selection
    assert result["selection"]["policy"] == "forecast-selection-v1"
    assert result["selection"]["promotion_id"] == (promotion.id if promotion else None)
    assert result["selection"]["model_artifact"] == expected.artifact
    assert all(result[key] == value for key, value in original_pin.items() if key != "state")
    saved = stored_job(job.id)
    assert saved.execution == result and canonical(saved.payload) == original_payload


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["unavailable", "foreign_experiment"])
def test_selected_invalid_promotion_is_an_error_without_baseline_fallback(catalogue, owned_jobs, invalid):
    case = catalogue
    case.model()  # An available fallback exists, so silent replacement would be observable.
    if invalid == "unavailable":
        promoted = case.model(status="insufficient_data")
    else:
        identity = "foreign_resolution_" + uuid4().hex[:12]
        foreign = ensure_experiment(
            ExperimentSpec(name=identity, provider="synthetic", west=140, east=141, south=35, north=36)
        )
        promoted = case.model(experiment_id=foreign.id)
    case.promote(promoted.id)
    payload = {"experiment_id": case.experiment.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    before = canonical(job.execution)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        with pytest.raises(ValueError, match="unavailable|incompatible"):
            resolve(owner, attempt, payload)
    assert canonical(stored_job(job.id).execution) == before


@pytest.mark.integration
def test_initial_legacy_manual_job_uses_created_time_not_resolution_clock(catalogue, owned_jobs):
    case = catalogue
    model = case.model()
    created_at = case.wall - timedelta(minutes=5)
    payload = {"experiment_id": case.experiment.id, "model_id": model.id}
    job = owned_jobs(payload, created_at, legacy=True)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        assert attempt.attempts == 1
        result = resolve(owner, attempt, payload)
    assert result["target_basis"] == "legacy_request_time"
    assert utc(result["request_time"]) == utc(result["input_cutoff"]) == created_at
    assert utc(result["start"]) == created_at + timedelta(minutes=30)
    assert stored_job(job.id).payload == payload


@pytest.mark.integration
@pytest.mark.parametrize("missing", ["experiment_id", "model_id", "start"])
def test_ambiguous_legacy_retry_is_typed_identity_unknown_without_inference(
    catalogue, owned_jobs, monkeypatch, missing
):
    case = catalogue
    model = case.model()
    payload = {"experiment_id": case.experiment.id, "model_id": model.id, "start": case.start.isoformat()}
    payload.pop(missing)
    job = owned_jobs(payload, case.wall, legacy=True, attempts=1)
    monkeypatch.setattr(
        forecast_execution, "real_experiment", lambda: pytest.fail("Ambiguous legacy retry inferred a source")
    )
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        assert attempt.attempts == 2
        with pytest.raises(LegacyRetryIdentityUnknown) as failure:
            resolve(owner, attempt, payload)
    assert failure.value.code == "legacy_retry_identity_unknown"
    assert failure.value.result["job_id"] == job.id
    assert stored_job(job.id).execution is None


@pytest.mark.integration
def test_full_explicit_legacy_retry_recovers_exact_archive_before_deadline_or_model_checks(
    catalogue, owned_jobs, monkeypatch
):
    case = catalogue
    model = case.model()
    cover_for_publication(case)
    archived = archive.issue_forecast(case.experiment.id, model.id, case.start)
    payload = {"experiment_id": case.experiment.id, "model_id": model.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall, legacy=True, attempts=1)
    monkeypatch.setattr(forecast_execution, "database_now", lambda _session: case.start + timedelta(days=1))
    monkeypatch.setattr(
        forecast_execution,
        "select_forecast_model",
        lambda *_args: pytest.fail("Recovery selected a new model"),
    )
    monkeypatch.setattr(
        forecast_execution,
        "_validate_model",
        lambda *_args: pytest.fail("Recovery revalidated model availability"),
    )
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        result = resolve(owner, attempt, payload)
    assert result["state"] == "resolved" and result["target_basis"] == "legacy_explicit_start"
    assert result["model_id"] == model.id and result["forecast_key"] == archived.logical_key
    assert result["selection"]["kind"] == "requested"
    assert stored_job(job.id).execution == result
    with transaction() as session:
        assert list(
            session.scalars(select(Forecast.id).where(Forecast.experiment_id == case.experiment.id))
        ) == [archived.id]


@pytest.mark.integration
def test_expired_target_is_typed_missed_before_model_lookup(catalogue, owned_jobs, monkeypatch):
    case = catalogue
    payload = {"experiment_id": case.experiment.id, "start": (case.wall - timedelta(seconds=1)).isoformat()}
    job = owned_jobs(payload, case.wall)
    monkeypatch.setattr(
        forecast_execution,
        "select_forecast_model",
        lambda *_args: pytest.fail("Expired target selected a model"),
    )
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        with pytest.raises(PublicationDeadlineMissed) as failure:
            resolve(owner, attempt, payload)
    assert failure.value.code == "publication_deadline_missed"
    assert failure.value.result["stage"] == "before model selection"
    assert utc(failure.value.result["start"]) == utc(payload["start"])
    assert stored_job(job.id).execution == job.execution


@pytest.mark.integration
def test_stale_resolver_cannot_write_identity_but_current_attempt_can(catalogue, owned_jobs):
    case = catalogue
    case.model()
    payload = {"experiment_id": case.experiment.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    with worker_session() as owner:
        old = jobs.claim(owner)
        with Session.begin() as session:
            session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(lease_until=text("clock_timestamp() - interval '1 second'"))
            )
        current = jobs.claim(owner)
        assert current.attempt_token != old.attempt_token
        before = canonical(stored_job(job.id).execution)
        with pytest.raises(JobOwnershipLost):
            resolve(owner, old, payload)
        assert canonical(stored_job(job.id).execution) == before
        result = resolve(owner, current, payload)
        assert result["state"] == "resolved" and stored_job(job.id).execution == result


@pytest.mark.integration
def test_concurrent_current_resolvers_return_one_immutable_resolution(catalogue, owned_jobs):
    case = catalogue
    case.model()
    payload = {"experiment_id": case.experiment.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    barrier = Barrier(3)
    with worker_session() as owner:
        attempt = jobs.claim(owner)

        def concurrent():
            barrier.wait(timeout=10)
            return resolve(owner, attempt, payload)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(concurrent) for _ in range(3)]
            results = [future.result(timeout=15) for future in futures]
    assert results == [results[0]] * 3
    assert results[0]["state"] == "resolved" and stored_job(job.id).execution == results[0]


@pytest.mark.integration
def test_target_only_pin_cannot_publish_until_complete_resolution(catalogue, owned_jobs):
    case = catalogue
    model = case.model()
    cover_for_publication(case)
    payload = {"experiment_id": case.experiment.id, "model_id": model.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        with (
            pytest.raises(ValueError, match="must be resolved before publication"),
            execution_scope(owner, job_id=job.id, attempt_token=attempt.attempt_token),
        ):
            archive.issue_forecast(case.experiment.id, model.id, case.start)
        with transaction() as session:
            assert not list(
                session.scalars(select(Forecast.id).where(Forecast.experiment_id == case.experiment.id))
            )
        result = resolve(owner, attempt, payload)
        with execution_scope(owner, job_id=job.id, attempt_token=attempt.attempt_token):
            with pytest.raises(ValueError, match="does not match its resolved execution"):
                archive.issue_forecast(case.experiment.id, model.id, case.start + timedelta(days=1))
            forecast = archive.issue_forecast(case.experiment.id, model.id, case.start)
        assert forecast.logical_key == result["forecast_key"]
        with transaction() as session:
            assert list(session.scalars(select(ForecastCell).where(ForecastCell.forecast_id == forecast.id)))


@pytest.mark.integration
def test_database_rejects_pinned_target_and_resolved_model_mutation(catalogue, owned_jobs):
    case = catalogue
    case.model()
    payload = {"experiment_id": case.experiment.id, "start": case.start.isoformat()}
    job = owned_jobs(payload, case.wall)
    original_payload = canonical(job.payload)

    def reject(changed, expected):
        with pytest.raises(DBAPIError) as failure, Session.begin() as session:
            session.execute(update(Job).where(Job.id == job.id).values(execution=changed))
        assert failure.value.orig.diag.constraint_name == "forecast_execution_immutable"
        assert stored_job(job.id).execution == expected

    pin = deepcopy(job.execution)
    reject(dict(pin, start=(case.start + timedelta(minutes=1)).isoformat()), pin)
    reject({key: value for key, value in pin.items() if key != "end"}, pin)
    reject(None, pin)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        complete = resolve(owner, attempt, payload)  # Additive pinned→resolved transition remains valid.
    reject(dict(complete, model_id=str(uuid4())), complete)
    reject(dict(complete, state="target_pinned"), complete)
    reject(None, complete)
    assert canonical(stored_job(job.id).payload) == original_payload
