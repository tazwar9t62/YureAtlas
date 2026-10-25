"""Actual domain commits and singleton lifetimes in the owned PostGIS harness."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from yureatlas import archive, execution, jobs, scheduling
from yureatlas.artifacts import canonical, read
from yureatlas.catalogue import ensure_experiment, ingest_records, make_snapshot
from yureatlas.db import Session, engine, transaction
from yureatlas.execution import Cancelled, JobOwnershipLost, WorkerOwnershipLost, execution_scope
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import (
    Evaluation,
    EvaluationCell,
    Event,
    Experiment,
    Forecast,
    ForecastCell,
    Ingestion,
    Job,
    Model,
    Revision,
    SchedulerState,
    Snapshot,
    Source,
    now,
)
from yureatlas.timeutil import next_day, utc
from yureatlas.worker_locks import worker_session

pytestmark = pytest.mark.integration
START = utc("2024-07-01T15:00:00Z")


def record(identity, origin):
    return {
        "provider_id": identity,
        "origin_time": origin,
        "provider_updated": START + timedelta(days=10),
        "longitude": 140.5,
        "latitude": 35.5,
        "depth_km": 10,
        "magnitude": 4.2,
        "magnitude_type": "Mw",
        "status": "reviewed",
        "event_type": "earthquake",
    }


@pytest.fixture
def domain():
    identity = "execution_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(name=identity, provider="synthetic", west=140, east=141, south=35, north=36)
    )
    rows = [record(f"history-{n}", START - timedelta(days=10 - n)) for n in range(5)]
    rows.append(record("observation", START + timedelta(hours=6)))
    ingest_records(
        experiment.source_id,
        rows,
        START - timedelta(days=20),
        START + timedelta(days=4),
        {"fixture": identity},
    )
    model = archive.train_rate(experiment.id, START - timedelta(days=2), START - timedelta(days=20))
    assert model.status == "available"
    forecast = archive.issue_forecast(experiment.id, model.id, START, mode="synthetic_demo")
    return SimpleNamespace(experiment=experiment, model=model, forecast=forecast, identity=identity)


@pytest.fixture
def running_job():
    job = jobs.enqueue("ping", {"fixture": "execution-fence"}, "execution:" + uuid4().hex)

    def owned_claims(state):
        # Retain actual locking/claim predicates and skip-locked ordering while
        # excluding jobs left by other acceptance cases in this owned database.
        if state.is_select and state.statement._for_update_arg is not None:
            entities = [item.get("entity") for item in state.statement.column_descriptions]
            if Job in entities:
                state.statement = state.statement.where(Job.id == job.id)

    event.listen(Session, "do_orm_execute", owned_claims)
    try:
        with worker_session() as owner:
            claimed = jobs.claim(owner)
            assert claimed.id == job.id and claimed.attempt_token
            yield SimpleNamespace(owner=owner, job=claimed)
    finally:
        event.remove(Session, "do_orm_execute", owned_claims)
        # Only this fixture's mutable operational row is terminalized. Research
        # rows, other jobs and the installation's scheduling cursor are intact.
        with Session.begin() as session:
            row = session.get(Job, job.id)
            if row.status in {"queued", "running"}:
                row.status = "cancelled"
                row.cancel_requested = True
                row.lease_until = None


def model_write(case):
    return archive.save_model(
        case.experiment,
        "historical_rate",
        archive.load_model(case.model),
        case.model.training_cutoff,
        case.model.snapshot_id,
        {"fixture": case.identity, "purpose": "guarded-publication"},
    )


def operation(case, kind):
    experiment = case.experiment
    if kind == "catalogue":
        return Revision, lambda: ingest_records(
            experiment.source_id,
            [record("new-event", START + timedelta(hours=12))],
            START,
            START + timedelta(days=1),
            {"fixture": case.identity, "purpose": "guarded-revision"},
        )
    if kind == "snapshot":
        cutoff = now()
        return Snapshot, lambda: make_snapshot(experiment.source_id, cutoff, "execution-fence")
    if kind == "model":
        return Model, lambda: model_write(case)
    if kind == "forecast":
        return Forecast, lambda: archive.issue_forecast(
            experiment.id, case.model.id, START + timedelta(days=2), mode="synthetic_demo"
        )
    return Evaluation, lambda: archive.evaluate_forecast(case.forecast.id)


def stored_domain(case, kind):
    experiment = case.experiment
    forecast_ids = select(Forecast.id).where(Forecast.experiment_id == experiment.id)
    evaluation_ids = select(Evaluation.id).where(Evaluation.forecast_id.in_(forecast_ids))
    event_ids = select(Event.id).where(Event.source_id == experiment.source_id)
    queries = {
        "catalogue": [
            (Source, Source.id == experiment.source_id),
            (Event, Event.source_id == experiment.source_id),
            (Revision, Revision.event_id.in_(event_ids)),
            (Ingestion, Ingestion.source_id == experiment.source_id),
        ],
        "snapshot": [(Snapshot, Snapshot.source_id == experiment.source_id)],
        "model": [(Model, Model.experiment_id == experiment.id)],
        "forecast": [
            (Forecast, Forecast.experiment_id == experiment.id),
            (ForecastCell, ForecastCell.forecast_id.in_(forecast_ids)),
        ],
        "evaluation": [
            (Evaluation, Evaluation.forecast_id.in_(forecast_ids)),
            (EvaluationCell, EvaluationCell.evaluation_id.in_(evaluation_ids)),
        ],
    }
    with transaction() as session:
        return [
            canonical(
                [
                    dict(row)
                    for row in session.execute(
                        select(table.__table__).where(where).order_by(*table.__table__.primary_key)
                    ).mappings()
                ]
            )
            for table, where in queries[kind]
        ]


@contextmanager
def after_first_insert(table, callback):
    observed = []
    caller = threading.get_ident()

    def after_execute(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            threading.get_ident() == caller
            and not observed
            and statement.startswith(f"INSERT INTO {table.__tablename__} ")
        ):
            observed.append(statement)
            callback()

    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        yield observed
    finally:
        event.remove(engine, "after_cursor_execute", after_execute)


@pytest.mark.parametrize("kind", ["catalogue", "snapshot", "model", "forecast", "evaluation"])
def test_reclaimed_attempt_cannot_commit_already_staged_domain_rows(domain, running_job, kind):
    case = running_job
    table, publish = operation(domain, kind)
    before = stored_domain(domain, kind)
    artifact_before = read(domain.model.artifact)
    replacement = []

    def reclaim():
        # This separate thread has no inherited execution ContextVar. Expiry is
        # fixture setup; the actual production claim rotates the attempt token.
        with Session.begin() as session:
            session.get(Job, case.job.id).lease_until = now() - timedelta(seconds=1)
        replacement.append(jobs.claim(case.owner))

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        after_first_insert(table, lambda: pool.submit(reclaim).result(timeout=10)) as inserted,
        pytest.raises(JobOwnershipLost),
        execution_scope(case.owner, job_id=case.job.id, attempt_token=case.job.attempt_token),
    ):
        publish()
    assert len(inserted) == len(replacement) == 1, "The test must reach a real staged domain INSERT"
    current = replacement[0]
    assert current.id == case.job.id and current.attempt_token != case.job.attempt_token
    assert current.attempts == case.job.attempts + 1
    assert stored_domain(domain, kind) == before
    assert read(domain.model.artifact) == artifact_before
    # The very same valid operation succeeds under its new attempt. Snapshot
    # commits before loss and orphan content-addressed bytes are not rolled back.
    with execution_scope(case.owner, job_id=current.id, attempt_token=current.attempt_token):
        publish()
    assert stored_domain(domain, kind) != before


def test_cancellation_acknowledged_before_guard_rolls_back_staged_model(domain, running_job):
    case = running_job
    before = stored_domain(domain, "model")
    acknowledgments = []

    def cancel():
        acknowledgments.append(jobs.request_cancel(case.job.id))

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        after_first_insert(Model, lambda: pool.submit(cancel).result(timeout=10)) as inserted,
        pytest.raises(Cancelled),
        execution_scope(case.owner, job_id=case.job.id, attempt_token=case.job.attempt_token),
    ):
        model_write(domain)
    assert len(inserted) == len(acknowledgments) == 1
    assert acknowledgments[0].cancel_requested
    assert stored_domain(domain, "model") == before


def wait_for_database_block(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blocked = connection.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid})
        if blocked:
            return
        time.sleep(0.01)
    pytest.fail("Expected the acknowledgment transaction to wait for the actual publication row lock")


def test_guarded_model_commit_finishes_before_cancellation_acknowledgment(domain, running_job, monkeypatch):
    case = running_job
    guard_held = threading.Event()
    release = threading.Event()
    cancel_sql = threading.Event()
    cancel_pid = []
    cancel_thread = []
    staged = []
    original_guard = execution.verify_execution

    def hold_real_guard(session):
        original_guard(session)
        context = execution.current_execution()
        if staged and context is not None and context.job_id == case.job.id:
            guard_held.set()
            assert release.wait(10), "Test did not release the guarded publication"

    def observe_sql(connection, _cursor, statement, _parameters, _context, _many):
        if cancel_thread and threading.get_ident() == cancel_thread[0] and "FOR UPDATE" in statement:
            cancel_pid.append(connection.connection.driver_connection.info.backend_pid)
            cancel_sql.set()

    def publish():
        with (
            after_first_insert(Model, lambda: staged.append(True)),
            execution_scope(case.owner, job_id=case.job.id, attempt_token=case.job.attempt_token),
        ):
            return model_write(domain)

    def cancel():
        cancel_thread.append(threading.get_ident())
        return jobs.request_cancel(case.job.id)

    monkeypatch.setattr(execution, "verify_execution", hold_real_guard)
    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(publish)
            try:
                assert guard_held.wait(10), "Publication did not acquire its real ownership guard"
                cancellation = pool.submit(cancel)
                assert cancel_sql.wait(5), "Cancellation did not reach its locking database query"
                wait_for_database_block(cancel_pid[0])
                assert not cancellation.done()
            finally:
                release.set()
            saved = publication.result(timeout=10)
            acknowledged = cancellation.result(timeout=10)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert acknowledged.cancel_requested
    with transaction() as session:
        assert session.get(Model, saved.id) is not None
    before = stored_domain(domain, "model")
    with (
        pytest.raises(Cancelled),
        execution_scope(case.owner, job_id=case.job.id, attempt_token=case.job.attempt_token),
    ):
        model_write(domain)
    assert stored_domain(domain, "model") == before


def test_worker_singleton_is_idle_exclusive_and_physically_released():
    with worker_session() as owner:
        with transaction() as session:
            activity = session.execute(
                text("SELECT state, xact_start, backend_start FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": owner.pid},
            ).one()
            assert activity.state == "idle" and activity.xact_start is None
            assert activity.backend_start == owner.backend_start
            execution.verify_worker(session, owner)
        with pytest.raises(WorkerOwnershipLost, match="already running"), worker_session():
            pytest.fail("A second singleton session was acknowledged")
        with transaction() as session:
            execution.verify_worker(session, owner)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with transaction() as session:
            exists = session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND backend_start = :start)"
                ),
                {"pid": owner.pid, "start": owner.backend_start},
            )
        if not exists:
            break
        time.sleep(0.01)
    assert not exists, "The physical singleton session was retained in the connection pool"
    with worker_session() as successor:
        assert successor.token != owner.token
        assert (successor.pid, successor.backend_start) != (owner.pid, owner.backend_start)


def test_terminated_singleton_fences_old_epoch_and_preserves_primary_cleanup_error(domain):
    primary = RuntimeError("Primary owned-worker task failure")
    with pytest.raises(RuntimeError) as failure, worker_session() as old:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            assert connection.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": old.pid})
        before = stored_domain(domain, "model")
        with worker_session() as successor:
            assert successor.token != old.token
            with pytest.raises(WorkerOwnershipLost), execution_scope(old):
                model_write(domain)
            assert stored_domain(domain, "model") == before
            with execution_scope(successor):
                saved = model_write(domain)
            with transaction() as session:
                assert session.get(Model, saved.id) is not None
        raise primary
    assert failure.value is primary
    assert any("Worker session cleanup failed" in note for note in primary.__notes__)
    # Failed unlock on the terminated connection cannot leak a pooled lock.
    with worker_session() as final, transaction() as session:
        execution.verify_worker(session, final)


def test_successor_epoch_acknowledgment_waits_for_already_guarded_publication(domain, monkeypatch):
    guard_held = threading.Event()
    release_publication = threading.Event()
    registration_sql = threading.Event()
    successor_acknowledged = threading.Event()
    release_successor = threading.Event()
    successor_thread, successor_pid, staged = [], [], []
    original_guard = execution.verify_execution
    primary = RuntimeError("Expected old singleton cleanup after controlled termination")

    def hold_real_guard(session):
        original_guard(session)
        if staged and execution.current_execution() is not None and not guard_held.is_set():
            guard_held.set()
            assert release_publication.wait(10), "Test did not release the authorized commit"

    def observe_registration(connection, _cursor, statement, _parameters, _context, _many):
        if (
            successor_thread
            and threading.get_ident() == successor_thread[0]
            and "worker_state" in statement
            and "FOR UPDATE" in statement
        ):
            successor_pid.append(connection.connection.driver_connection.info.backend_pid)
            registration_sql.set()

    def publish(owner):
        with after_first_insert(Model, lambda: staged.append(True)), execution_scope(owner):
            return model_write(domain)

    def take_over():
        successor_thread.append(threading.get_ident())
        with worker_session() as successor:
            successor_acknowledged.set()
            assert release_successor.wait(10), "Test did not release the successor singleton"
            return successor

    monkeypatch.setattr(execution, "verify_execution", hold_real_guard)
    event.listen(engine, "before_cursor_execute", observe_registration)
    try:
        with pytest.raises(RuntimeError) as failure, worker_session() as old:
            with ThreadPoolExecutor(max_workers=2) as pool:
                publication = pool.submit(publish, old)
                try:
                    assert guard_held.wait(10)
                    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                        assert connection.scalar(
                            text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": old.pid}
                        )
                    takeover = pool.submit(take_over)
                    assert registration_sql.wait(5)
                    wait_for_database_block(successor_pid[0])
                    assert not successor_acknowledged.is_set()
                    release_publication.set()
                    saved = publication.result(timeout=10)
                    assert successor_acknowledged.wait(5)
                    with transaction() as session:
                        assert session.get(Model, saved.id) is not None
                    before = stored_domain(domain, "model")
                    with pytest.raises(WorkerOwnershipLost), execution_scope(old):
                        model_write(domain)
                    assert stored_domain(domain, "model") == before
                finally:
                    release_publication.set()
                    release_successor.set()
                successor = takeover.result(timeout=10)
                assert successor.token != old.token
            raise primary
        assert failure.value is primary
        assert any("Worker session cleanup failed" in note for note in primary.__notes__)
    finally:
        release_publication.set()
        release_successor.set()
        event.remove(engine, "before_cursor_execute", observe_registration)


def test_scheduler_thread_fences_old_owner_jobs_and_cursor_then_successor_recovers(monkeypatch):
    identity = "scheduler_fence_" + uuid4().hex[:12]
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
    state_id = "scheduler-fence-" + uuid4().hex
    target, _ = next_day(experiment.created_at + timedelta(days=1))
    clock = {"value": target - timedelta(minutes=15)}
    original_experiments = scheduling.real_experiments

    def selected(*, session):
        scoped = SimpleNamespace(
            scalars=lambda statement: session.scalars(statement.where(Experiment.id == experiment.id))
        )
        return original_experiments(session=scoped)

    def stored():
        with transaction() as session:
            rows = session.execute(
                select(Job.__table__)
                .where(Job.payload["experiment_id"].as_string() == experiment.id)
                .order_by(Job.id)
            ).mappings()
            state = (
                session.execute(select(SchedulerState.__table__).where(SchedulerState.id == state_id))
                .mappings()
                .one()
            )
            return {row["id"]: canonical(dict(row)) for row in rows}, canonical(dict(state))

    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
    monkeypatch.setattr(scheduling, "STATE_ID", state_id)
    monkeypatch.setattr(scheduling, "real_experiments", selected)
    monkeypatch.setattr(scheduling, "now", lambda: clock["value"])
    with worker_session() as old, ThreadPoolExecutor(max_workers=1) as pool:
        old_timer = scheduling.ReconciliationWatchdog(owner=old)
        pool.submit(old_timer._check).result(timeout=10)
        assert not old_timer._stop.is_set()
    baseline_jobs, baseline_state = stored()
    assert baseline_jobs
    clock["value"] = target + timedelta(days=2, hours=12)
    staged = []

    def stale_tick():
        assert execution.current_execution() is None
        with after_first_insert(Job, lambda: staged.append("jobs")):
            old_timer._check()  # The real timer must bind its owner in this new thread.
        assert execution.current_execution() is None

    def observe_cursor(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE scheduler_state "):
            staged.append("cursor")

    with worker_session() as successor, ThreadPoolExecutor(max_workers=1) as pool:
        event.listen(engine, "after_cursor_execute", observe_cursor)
        try:
            pool.submit(stale_tick).result(timeout=10)
        finally:
            event.remove(engine, "after_cursor_execute", observe_cursor)
        assert staged == ["jobs", "cursor"], "Both scheduler mutations must reach the final ownership fence"
        assert old_timer._stop.is_set()
        assert stored() == (baseline_jobs, baseline_state)
        current_timer = scheduling.ReconciliationWatchdog(owner=successor)
        pool.submit(current_timer._check).result(timeout=10)
        assert not current_timer._stop.is_set()
        current_jobs, current_state = stored()
        assert len(current_jobs) > len(baseline_jobs) and current_state != baseline_state
        assert all(current_jobs[key] == value for key, value in baseline_jobs.items())
        with transaction() as session:
            assert session.get(SchedulerState, state_id).observed_at == clock["value"]
