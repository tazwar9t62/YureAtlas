"""Actual dispatch/finalization paths inside a harness-owned worker session."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session as OrmSession
from yureatlas import jobs, worker
from yureatlas.artifacts import canonical
from yureatlas.catalogue import ensure_experiment, ingest_records
from yureatlas.db import Session, engine, transaction
from yureatlas.execution import WorkerOwnershipLost, current_execution
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Event, Ingestion, Job, Revision
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session

pytestmark = pytest.mark.integration


@pytest.fixture
def queued_job():
    job = jobs.enqueue("ping", {"fixture": "worker-dispatch"}, "worker-dispatch:" + uuid4().hex)

    def owned_locks(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id == job.id)

    event.listen(OrmSession, "do_orm_execute", owned_locks)
    try:
        yield job
    finally:
        event.remove(OrmSession, "do_orm_execute", owned_locks)
        with Session.begin() as session:
            session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status.in_(["queued", "running"]))
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )


def read_job(identity):
    with Session.begin() as session:
        return session.get(Job, identity)


def stored_job(identity):
    with Session.begin() as session:
        return canonical(
            dict(session.execute(select(Job.__table__).where(Job.id == identity)).mappings().one())
        )


@contextmanager
def cancel_after_revision_insert(job_id):
    caller = threading.get_ident()
    staged = []

    def after_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            not staged
            and threading.get_ident() == caller
            and statement.startswith("INSERT INTO earthquake_revisions")
        ):
            staged.append(True)
            assert jobs.request_cancel(job_id).status == "running"

    event.listen(engine, "after_cursor_execute", after_insert)
    try:
        yield staged
    finally:
        event.remove(engine, "after_cursor_execute", after_insert)


def test_run_once_completes_real_ping_dispatch_and_has_no_second_job(queued_job):
    with worker_session() as owner:
        assert worker.run_once(owner) is True
        result = read_job(queued_job.id)
        assert result.status == "completed" and result.result == {"message": "Worker is ready"}
        assert result.progress == 1 and result.attempts == 1
        assert result.attempt_token and result.worker_token == owner.token and result.lease_until is None
        assert current_execution() is None
        original = stored_job(queued_job.id)
        assert worker.run_once(owner) is False
        assert stored_job(queued_job.id) == original


def test_run_once_cancellation_keeps_earlier_commit_and_rolls_back_later_staged_receipt(
    queued_job, monkeypatch
):
    identity = "dispatch_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="synthetic",
            west=140,
            east=141,
            south=35,
            north=36,
        )
    )
    start = utc("2024-01-01T00:00:00Z")

    def record(provider_id):
        return {
            "provider_id": provider_id,
            "origin_time": start,
            "provider_updated": start,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": 10,
            "magnitude": 4.2,
            "magnitude_type": "synthetic",
            "status": "reviewed",
            "event_type": "earthquake",
        }

    staged = []

    def dispatch(_kind, _payload, progress):
        progress(0.25)
        ingest_records(
            experiment.source_id, [record("earlier")], start, start + timedelta(days=1), {"phase": "earlier"}
        )
        progress(0.6)
        with cancel_after_revision_insert(queued_job.id) as observed:
            staged.append(observed)
            ingest_records(
                experiment.source_id,
                [record("later")],
                start,
                start + timedelta(days=1),
                {"phase": "later"},
            )
        pytest.fail("Cancelled dispatch returned past the final publication guard")

    monkeypatch.setattr(worker, "dispatch", dispatch)
    with worker_session() as owner:
        assert worker.run_once(owner) is True
    assert staged == [[True]]
    result = read_job(queued_job.id)
    assert result.status == "cancelled" and result.cancel_requested and result.progress == 0.6
    assert result.result == {} and result.lease_until is None
    assert current_execution() is None
    with transaction() as session:
        events = list(session.scalars(select(Event).where(Event.source_id == experiment.source_id)))
        revisions = list(
            session.scalars(select(Revision).join(Event).where(Event.source_id == experiment.source_id))
        )
        receipts = list(session.scalars(select(Ingestion).where(Ingestion.source_id == experiment.source_id)))
    assert len(events) == len(revisions) == len(receipts) == 1
    assert events[0].provider_id == "earlier"


def test_run_once_stale_return_cannot_overwrite_reclaimed_attempt_completion(queued_job, monkeypatch):
    successor = []

    def dispatch(_kind, _payload, progress):
        progress(0.2)
        context = current_execution()
        # Simulate another control caller after this attempt stops renewing.
        # Control sessions intentionally do not inherit the old domain guard.
        with Session.begin() as session:
            session.execute(
                update(Job)
                .where(Job.id == queued_job.id)
                .values(lease_until=text("clock_timestamp() - interval '1 second'"))
            )
        reclaimed = jobs.claim(context.owner)
        assert reclaimed.id == queued_job.id and reclaimed.attempt_token != context.attempt_token
        jobs.finish(
            reclaimed.id, reclaimed.attempt_token, context.owner, "completed", result={"successor": True}
        )
        successor.append(stored_job(queued_job.id))
        return {"stale": "must not overwrite the successor"}

    monkeypatch.setattr(worker, "dispatch", dispatch)
    with worker_session() as owner:
        assert worker.run_once(owner) is True
    assert successor == [stored_job(queued_job.id)]
    result = read_job(queued_job.id)
    assert result.status == "completed" and result.attempts == 2 and result.result == {"successor": True}
    assert current_execution() is None


def test_run_once_propagates_singleton_loss_instead_of_finishing_the_job(queued_job, monkeypatch):
    original = []

    def dispatch(_kind, _payload, progress):
        progress(0.2)
        context = current_execution()
        original.append(stored_job(queued_job.id))
        with Session.begin() as session:
            assert session.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": context.owner.pid})
        return {"unowned": "cannot finish"}

    monkeypatch.setattr(worker, "dispatch", dispatch)
    with pytest.raises(WorkerOwnershipLost), worker_session() as owner:
        worker.run_once(owner)
    assert original == [stored_job(queued_job.id)]
    result = read_job(queued_job.id)
    assert result.status == "running" and result.progress == 0.2 and result.result == {}
    assert current_execution() is None


def test_background_heartbeat_preserves_attempt_progress_and_cancels_blocked_dispatch(
    queued_job, monkeypatch
):
    dispatch_ready = threading.Event()
    renewed = threading.Event()
    cancellation_seen = threading.Event()
    heartbeat_finished = threading.Event()
    pulses, expected_identity, final_statuses = [], [], []
    real_checkpoint, real_finish = worker.checkpoint, worker.finish

    class ObservedPool(ThreadPoolExecutor):
        def submit(self, function, /, *args, **kwargs):
            future = super().submit(function, *args, **kwargs)
            # Future completion occurs after the real heartbeat loop records
            # its interruption, eliminating a race with dispatch's return.
            future.add_done_callback(lambda _future: heartbeat_finished.set())
            return future

    def checkpoint(job_id, attempt_token, owner, progress=None):
        during_dispatch = dispatch_ready.is_set()
        try:
            result = real_checkpoint(job_id, attempt_token, owner, progress)
        except jobs.Cancelled:
            if progress is None:
                cancellation_seen.set()
            raise
        if progress is None and during_dispatch:
            pulses.append((job_id, attempt_token, owner.token, result.progress))
            renewed.set()
        return result

    def finish(job_id, attempt_token, owner, status, *args, **kwargs):
        final_statuses.append(status)
        return real_finish(job_id, attempt_token, owner, status, *args, **kwargs)

    def dispatch(_kind, _payload, progress):
        progress(0.4)
        context = current_execution()
        expected_identity.append((context.job_id, context.attempt_token, context.owner.token, 0.4))
        dispatch_ready.set()
        assert renewed.wait(timeout=5), "Heartbeat did not renew while dispatch was blocked"
        assert jobs.request_cancel(queued_job.id).status == "running"
        assert cancellation_seen.wait(timeout=5), "Heartbeat did not observe cancellation"
        assert heartbeat_finished.wait(timeout=5), "Heartbeat failed to finish after cancellation"
        return {"stale": "discard after heartbeat interruption"}

    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(worker, "ThreadPoolExecutor", ObservedPool)
    monkeypatch.setattr(worker, "checkpoint", checkpoint)
    monkeypatch.setattr(worker, "finish", finish)
    monkeypatch.setattr(worker, "dispatch", dispatch)
    with worker_session() as owner:
        assert worker.run_once(owner) is True
    assert pulses and all(pulse == expected_identity[0] for pulse in pulses)
    # This proves runner interruption handling, not merely finish() converting
    # an attempted completion after noticing the persisted cancellation flag.
    assert final_statuses == ["cancelled"]
    result = read_job(queued_job.id)
    assert result.status == "cancelled" and result.progress == 0.4 and result.result == {}
    assert result.lease_until is None and current_execution() is None
