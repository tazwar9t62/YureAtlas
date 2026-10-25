"""Durable forecast identity through real worker and prospective archive paths."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import Session as OrmSession
from yureatlas import archive, forecast_execution, jobs, worker
from yureatlas import schedule_configuration as configuration
from yureatlas.artifacts import canonical, put, read
from yureatlas.catalogue import ensure_experiment, ingest_records, load_experiment, make_snapshot
from yureatlas.config import settings
from yureatlas.db import Session, transaction
from yureatlas.forecast_errors import PublicationDeadlineMissed
from yureatlas.rate import HistoricalRate
from yureatlas.schedule_schemas import ScheduleUpdate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Forecast, ForecastCell, Job, Promotion
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session

pytestmark = pytest.mark.integration


class SimulatedCrash(BaseException):
    """A process-like interruption that run_once must not classify as failure."""


@pytest.fixture
def owned_queue(monkeypatch):
    monkeypatch.setattr(settings, "schedule_enabled", True)
    identities = set()
    experiment_ids = set()
    with transaction() as session:
        original_policy = configuration.load_schedule(session)["spec"]

    def save_policy(spec, reason):
        with transaction() as session:
            revision = configuration.load_schedule(session)["revision"]
        configuration.update_schedule(ScheduleUpdate(expected_revision=revision, spec=spec, reason=reason))

    def select_owned_experiments():
        save_policy(
            {
                "enabled": True,
                "scope": "selected",
                "experiment_ids": sorted(experiment_ids),
                "kinds": ["forecast"],
            },
            "Enable only owned forecast-recovery experiments",
        )

    def scoped_locks(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id.in_(identities))

    def enqueue(payload, key=None):
        # Manual/legacy requests remain manual. Scheduled requests still pass
        # through the actual persisted policy and claim predicate.
        if payload.get("scheduled") is True and payload.get("experiment_id") not in experiment_ids:
            experiment_ids.add(payload["experiment_id"])
            select_owned_experiments()
        job = jobs.enqueue("forecast", payload, key or "forecast-recovery:" + uuid4().hex)
        identities.add(job.id)
        return job

    event.listen(OrmSession, "do_orm_execute", scoped_locks)
    try:
        select_owned_experiments()
        yield enqueue
    finally:
        event.remove(OrmSession, "do_orm_execute", scoped_locks)
        with Session.begin() as session:
            session.execute(
                update(Job)
                .where(Job.id.in_(identities), Job.status.in_(["queued", "running"]))
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )
        # Preserve audit history, restoring the former request with a new
        # revision rather than mutating or deleting earlier configuration.
        save_policy(original_policy, "Restore policy after owned forecast recovery acceptance")


@pytest.fixture
def prospective(monkeypatch):
    identity = "forecast_retry_" + uuid4().hex[:12]
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
    monkeypatch.setattr(forecast_execution, "database_now", lambda _session: clock.value)
    monkeypatch.setattr(forecast_execution, "now", lambda: clock.value)
    monkeypatch.setattr(jobs, "now", lambda: clock.value)
    training_cutoff = wall - timedelta(days=2)
    snapshot, _ = make_snapshot(experiment.source_id, training_cutoff, "retrospective_revised")
    models, fitted = [], []
    for index, rate in enumerate((0.02, 0.09)):
        artifact = HistoricalRate(np.full(len(grid.ids), rate), 1.4, 0.05, 5)
        artifact.fixture_identity = f"{identity}:{index}"  # Never share deliberately damaged bytes.
        clock.value = wall - timedelta(hours=2 - index)
        model = archive.save_model(
            experiment,
            "historical_rate",
            artifact,
            training_cutoff,
            snapshot.id,
            {"feature_schema": "rate-v1"},
        )
        assert read(model.artifact)
        np.testing.assert_array_equal(archive.load_model(model).predict(), artifact.predict())
        models.append(model)
        fitted.append(artifact)
    clock.value = wall
    start, cutoff = wall + timedelta(minutes=15), wall - timedelta(minutes=15)
    raw = b"provider_id,origin_time,provider_updated,longitude,latitude,depth_km,magnitude,magnitude_type,status,event_type\n"
    ingest_records(
        experiment.source_id,
        [],
        cutoff - timedelta(days=1),
        cutoff - timedelta(minutes=30),
        {},
        received_at=cutoff,
        status="complete",
        import_identity={
            "catalogue_namespace": identity,
            "namespace": "coverage_" + uuid4().hex,
            "format": "csv",
            "artifact": put(raw, "csv"),
            "line_count": 1,
            "mapping": {},
            "parser_version": "csv-v2",
            "coverage_declared": True,
        },
    )
    with transaction() as session:
        promotion = Promotion(
            experiment_id=experiment.id,
            model_id=models[0].id,
            created_at=wall - timedelta(minutes=1),
            reason="Initial fixture model selection",
        )
        session.add(promotion)
        session.flush()
    return SimpleNamespace(
        experiment=experiment,
        grid=grid,
        models=models,
        fitted=fitted,
        promotion=promotion,
        clock=clock,
        wall=wall,
        start=start,
        cutoff=cutoff,
        payload={"experiment_id": experiment.id, "start": start.isoformat(), "scheduled": True},
    )


def job_state(identity):
    with Session.begin() as session:
        return session.get(Job, identity)


def expire(identity):
    with Session.begin() as session:
        session.execute(
            update(Job)
            .where(Job.id == identity)
            .values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )


def promote_second(case):
    with transaction() as session:
        session.add(
            Promotion(
                experiment_id=case.experiment.id,
                model_id=case.models[1].id,
                created_at=case.clock.value,
                reason="A later promotion must affect future resolution only",
            )
        )


def archive_rows(case):
    with transaction() as session:
        forecasts = list(
            session.scalars(select(Forecast).where(Forecast.experiment_id == case.experiment.id))
        )
        cells = list(
            session.scalars(
                select(ForecastCell)
                .join(Forecast)
                .where(Forecast.experiment_id == case.experiment.id)
                .order_by(ForecastCell.cell_id)
            )
        )
    return forecasts, cells


def assert_one_original_forecast(case):
    forecasts, cells = archive_rows(case)
    assert len(forecasts) == 1
    forecast = forecasts[0]
    assert forecast.model_id == case.models[0].id
    assert forecast.start == case.start and forecast.input_cutoff == case.cutoff
    assert {cell.cell_id for cell in cells} == set(case.grid.ids)
    assert len(cells) == len(case.grid.ids)
    np.testing.assert_allclose([cell.probability for cell in cells], case.fitted[0].predict())
    return forecast


def test_retry_after_real_publication_crash_keeps_identity_after_deadline_promotion_and_corrupt_artifact(
    prospective, owned_queue, monkeypatch
):
    case = prospective
    queued = owned_queue(case.payload)
    publish = forecast_execution.issue_forecast

    def publish_then_crash(*args, **kwargs):
        forecast = publish(*args, **kwargs)
        assert forecast.id == assert_one_original_forecast(case).id
        raise SimulatedCrash("Published, but process disappeared before job completion")

    monkeypatch.setattr(forecast_execution, "issue_forecast", publish_then_crash)
    with worker_session() as owner, pytest.raises(SimulatedCrash):
        worker.run_once(owner)
    crashed = job_state(queued.id)
    original = assert_one_original_forecast(case)
    assert crashed.status == "running" and crashed.attempts == 1
    assert crashed.execution["model_id"] == case.models[0].id
    assert crashed.execution["selection"]["promotion_id"] == case.promotion.id
    resolution = canonical(crashed.execution)
    case.clock.value = case.start + timedelta(minutes=5)
    promote_second(case)
    expire(queued.id)
    monkeypatch.setattr(forecast_execution, "issue_forecast", publish)
    artifact_path = settings.artifact_dir / case.models[0].artifact
    original_bytes = read(case.models[0].artifact)
    try:
        artifact_path.write_bytes(b"Deliberately unreadable fixture artifact after publication")
        with worker_session() as successor:
            assert worker.run_once(successor) is True
    finally:
        artifact_path.write_bytes(original_bytes)
    completed = job_state(queued.id)
    assert completed.status == "completed" and completed.attempts == 2
    assert completed.result["forecast_id"] == original.id
    assert canonical(completed.execution) == resolution
    assert assert_one_original_forecast(case).id == original.id
    assert read(case.models[0].artifact) == original_bytes


def test_retry_after_resolution_crash_keeps_model_despite_later_promotion(
    prospective, owned_queue, monkeypatch
):
    case = prospective
    queued = owned_queue(case.payload)
    resolve = forecast_execution.resolve_forecast

    def resolve_then_crash(payload):
        resolved = resolve(payload)
        assert resolved["model_id"] == case.models[0].id
        raise SimulatedCrash("Resolution committed before the process disappeared")

    monkeypatch.setattr(forecast_execution, "resolve_forecast", resolve_then_crash)
    with worker_session() as owner, pytest.raises(SimulatedCrash):
        worker.run_once(owner)
    crashed = job_state(queued.id)
    assert crashed.status == "running" and not archive_rows(case)[0]
    resolution = canonical(crashed.execution)
    assert crashed.execution["model_id"] == case.models[0].id
    case.clock.value += timedelta(minutes=1)
    promote_second(case)
    expire(queued.id)
    monkeypatch.setattr(forecast_execution, "resolve_forecast", resolve)
    with worker_session() as successor:
        assert worker.run_once(successor) is True
    completed = job_state(queued.id)
    assert completed.status == "completed" and completed.attempts == 2
    assert canonical(completed.execution) == resolution
    assert completed.result["forecast_id"] == assert_one_original_forecast(case).id


def test_manual_enqueue_pins_target_once_without_rewriting_request_or_logical_key(prospective, owned_queue):
    case = prospective
    payload = {"experiment_id": case.experiment.id}
    original_payload = deepcopy(payload)
    key = "manual-identity:" + uuid4().hex
    first = owned_queue(payload, key)
    state = job_state(first.id)
    assert state.logical_key == key and state.payload == original_payload and payload == original_payload
    assert utc(state.execution["start"]) == case.wall + timedelta(minutes=30)
    assert utc(state.execution["input_cutoff"]) == case.wall
    assert state.execution.get("model_id") is None
    execution = canonical(state.execution)
    case.clock.value += timedelta(days=2)
    repeated = owned_queue(payload, key)
    assert repeated.id == first.id and repeated.created_at == first.created_at
    assert repeated.logical_key == key and repeated.payload == original_payload
    assert canonical(repeated.execution) == execution and payload == original_payload


@pytest.mark.parametrize("unavailable", ["no_model", "corrupt_artifact"])
def test_expired_scheduled_target_is_typed_missed_before_model_or_artifact_checks(
    prospective, owned_queue, unavailable
):
    case = prospective
    payload = deepcopy(case.payload)
    if unavailable == "no_model":
        identity = "empty_retry_" + uuid4().hex[:12]
        empty = ensure_experiment(
            ExperimentSpec(**{**case.experiment.config, "name": identity, "catalogue_namespace": identity})
        )
        payload["experiment_id"] = empty.id
    queued = owned_queue(payload)
    case.clock.value = case.start
    path = settings.artifact_dir / case.models[0].artifact
    original_bytes = read(case.models[0].artifact)
    try:
        if unavailable == "corrupt_artifact":
            path.write_bytes(b"Deliberately unreadable deadline fixture")
        with worker_session() as owner:
            assert worker.run_once(owner) is True
    finally:
        path.write_bytes(original_bytes)
    missed = job_state(queued.id)
    assert missed.status == "missed" and missed.result["code"] == "publication_deadline_missed"
    assert utc(missed.result["start"]) == case.start
    assert utc(missed.result["checked_at"]) == case.start
    assert not archive_rows(case)[0]


@pytest.mark.parametrize("typed", [False, True])
def test_worker_classifies_deadline_by_exception_type_instead_of_message(
    prospective, owned_queue, monkeypatch, typed
):
    case = prospective
    queued = owned_queue(case.payload)
    failure = (
        PublicationDeadlineMissed(case.start, "fixture boundary", case.clock.value)
        if typed
        else ValueError("An unrelated failure happened to mention a deadline")
    )
    if typed:
        failure.args = ("This custom explanation contains no timing keyword",)

    def dispatch(_kind, _payload, _progress):
        raise failure

    monkeypatch.setattr(worker, "dispatch", dispatch)
    with worker_session() as owner:
        assert worker.run_once(owner) is True
    result = job_state(queued.id)
    assert result.status == ("missed" if typed else "failed")
    if typed:
        assert result.result["code"] == "publication_deadline_missed"
    else:
        assert "deadline" in result.error and result.result == {}
    assert not archive_rows(case)[0]
