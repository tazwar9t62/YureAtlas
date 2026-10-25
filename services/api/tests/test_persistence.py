"""Integration invariants run only in a harness-owned disposable database."""

from datetime import timedelta

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as OrmSession
from yureatlas.archive import evaluate_forecast, issue_forecast, train_rate
from yureatlas.catalogue import ensure_experiment, ingest_records, revisions_asof
from yureatlas.db import transaction
from yureatlas.jobs import claim, enqueue, finish
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Evaluation, ForecastCell, Job, Revision, now
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session

pytestmark = pytest.mark.integration


@pytest.fixture
def experiment():
    from uuid import uuid4

    return ensure_experiment(
        ExperimentSpec(
            name="test_" + uuid4().hex[:12], provider="synthetic", west=140, east=142, south=35, north=37
        )
    )


def event(event_id, origin, **changes):
    return dict(
        {
            "provider_id": event_id,
            "origin_time": origin,
            "provider_updated": origin,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": None,
            "magnitude": 4.4,
            "magnitude_type": "synthetic",
            "status": "reviewed",
        },
        **changes,
    )


def test_revisions_dedup_deletion_and_cutoff(experiment):
    start = utc("2020-01-01T00:00:00Z")
    receipt = start + timedelta(days=1)
    first = event(experiment.name, start)
    ingest_records(experiment.source_id, [first, first], start, receipt, {}, received_at=receipt)
    deleted = dict(first, provider_updated=receipt, status="deleted")
    ingest_records(
        experiment.source_id, [deleted], start, receipt, {}, received_at=receipt + timedelta(days=1)
    )
    before = [r for r in revisions_asof(experiment.source_id, receipt) if r.raw_ref]
    assert any(r.origin_time == start for r in before)
    after = revisions_asof(experiment.source_id, receipt + timedelta(days=1))
    assert not any(r.id == before[-1].id for r in after)
    with transaction() as s:
        revision = s.scalar(select(Revision).where(Revision.id == before[-1].id))
        revision_id = revision.id
    with pytest.raises(DBAPIError), transaction() as s:
        s.execute(text("UPDATE earthquake_revisions SET magnitude=9 WHERE id=:id"), {"id": revision_id})


def test_atomic_immutable_forecast_and_versioned_evaluation(experiment):
    start = utc("2021-01-01T15:00:00Z")
    records = [event(f"{experiment.name}-{i}", start + timedelta(days=i)) for i in range(12)]
    ingest_records(
        experiment.source_id,
        records,
        start,
        start + timedelta(days=20),
        {},
        received_at=start + timedelta(days=20),
    )
    cutoff = start + timedelta(days=10)
    model = train_rate(experiment.id, cutoff, start)
    prediction = issue_forecast(experiment.id, model.id, start + timedelta(days=11), mode="synthetic_demo")
    retry = issue_forecast(experiment.id, model.id, start + timedelta(days=11), mode="synthetic_demo")
    assert prediction.id == retry.id
    with pytest.raises(DBAPIError), transaction() as s:
        s.execute(
            text("UPDATE forecast_cells SET probability=.99 WHERE forecast_id=:id"), {"id": prediction.id}
        )
    with pytest.raises(DBAPIError), transaction() as s:
        s.add(ForecastCell(forecast_id=prediction.id, cell_id="extra", probability=0.1, band="red"))
    first = evaluate_forecast(prediction.id)
    revised = dict(records[11], magnitude=3.1, provider_updated=now())
    ingest_records(experiment.source_id, [revised], start, start + timedelta(days=20), {})
    second = evaluate_forecast(prediction.id)
    assert second.version == first.version + 1
    assert second.metrics["positives"] == first.metrics["positives"] - 1
    with transaction() as s:
        assert s.get(Evaluation, first.id).metrics == first.metrics


def test_synthetic_experiment_rejects_real_forecast_mode(experiment):
    with pytest.raises(ValueError, match="Synthetic and real"):
        issue_forecast(experiment.id, "unknown", now() - timedelta(days=1))


def test_job_key_and_expired_lease(experiment):
    key = experiment.name + "-ping"
    first = enqueue("ping", {}, key)
    assert enqueue("ping", {}, key).id == first.id

    def restrict_job_locks(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id == first.id)

    sqlalchemy_event.listen(OrmSession, "do_orm_execute", restrict_job_locks)
    try:
        with worker_session() as owner:
            original = claim(owner)
            assert original.id == first.id
            with transaction() as s:
                s.execute(
                    update(Job)
                    .where(Job.id == first.id)
                    .values(lease_until=text("clock_timestamp() - interval '1 second'"))
                )
            reclaimed = claim(owner)
            assert reclaimed.id == first.id and reclaimed.attempts == 2
            assert reclaimed.attempt_token != original.attempt_token
            finish(reclaimed.id, reclaimed.attempt_token, owner, "completed")
    finally:
        sqlalchemy_event.remove(OrmSession, "do_orm_execute", restrict_job_locks)
        with transaction() as s:
            s.execute(
                update(Job)
                .where(Job.id == first.id, Job.status.in_(["queued", "running"]))
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )
