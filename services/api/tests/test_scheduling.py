"""Daily restart decisions; database cases require the owned PostGIS harness."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from threading import Event as ThreadEvent
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select, text
from yureatlas import jobs, scheduling
from yureatlas.artifacts import canonical
from yureatlas.catalogue import ensure_experiment
from yureatlas.db import engine, transaction
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Experiment, Job
from yureatlas.timeutil import JST, next_day, utc

TARGET = utc("2025-01-01T00:00:00+09:00")
CREATED = TARGET - timedelta(days=2)


@pytest.fixture(autouse=True)
def enabled_schedule(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)


@pytest.fixture
def captured_decisions(monkeypatch):
    calls = []

    def capture(status, kind, payload, key, reason=None):
        result = SimpleNamespace(status=status, kind=kind, payload=payload, logical_key=key, error=reason)
        calls.append(result)
        return result

    monkeypatch.setattr(scheduling, "enqueue", lambda *args: capture("queued", *args))
    monkeypatch.setattr(scheduling, "enqueue_missed", lambda *args: capture("missed", *args))
    return calls


@pytest.mark.parametrize(
    "minutes_before,status", [(21, None), (20, "queued"), (15, "queued"), (1, "queued"), (0, "missed")]
)
def test_daily_decision_uses_jst_2340_trigger_and_midnight_deadline(
    captured_decisions, minutes_before, status
):
    timestamp = TARGET - timedelta(minutes=minutes_before)
    result = scheduling.daily_forecast_decision("experiment", CREATED, TARGET, timestamp)
    if status is None:
        assert result is None and not captured_decisions
        return
    assert captured_decisions == [result]
    assert result.status == status and result.kind == "forecast"
    assert result.logical_key == f"daily:experiment:{TARGET.isoformat()}"
    assert result.payload == {"experiment_id": "experiment", "start": TARGET.isoformat(), "scheduled": True}
    if status == "missed":
        assert "deadline" in result.error


def test_equivalent_aware_offsets_have_identical_daily_identity(captured_decisions):
    timestamp = TARGET - timedelta(minutes=15)
    for timezone in (JST, ZoneInfo("America/Los_Angeles"), ZoneInfo("UTC")):
        scheduling.daily_forecast_decision(
            "experiment",
            CREATED.astimezone(timezone),
            TARGET.astimezone(timezone),
            timestamp.astimezone(timezone),
        )
    assert len(captured_decisions) == 3
    assert len({row.logical_key for row in captured_decisions}) == 1
    assert all(row.payload == captured_decisions[0].payload for row in captured_decisions)


@pytest.mark.parametrize("field", ["created_at", "start", "timestamp"])
def test_naive_instants_are_rejected_before_enqueue(captured_decisions, field):
    arguments = {"created_at": CREATED, "start": TARGET, "timestamp": TARGET - timedelta(minutes=15)}
    arguments[field] = arguments[field].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        scheduling.daily_forecast_decision("experiment", **arguments)
    assert not captured_decisions


def test_daily_decision_rejects_non_midnight_target(captured_decisions):
    with pytest.raises(ValueError, match="JST-midnight"):
        scheduling.daily_forecast_decision("experiment", CREATED, TARGET + timedelta(minutes=1), TARGET)
    assert not captured_decisions


@pytest.mark.parametrize("created_at", [TARGET, TARGET + timedelta(seconds=1)])
def test_experiment_created_at_or_after_target_has_no_historical_decision(captured_decisions, created_at):
    assert (
        scheduling.daily_forecast_decision("experiment", created_at, TARGET, TARGET + timedelta(days=1))
        is None
    )
    assert not captured_decisions


@pytest.mark.parametrize(
    "entrypoint",
    [
        "daily",
        "forecast_trigger",
        "reconcile_startup",
        "ingest_trigger",
        "evaluate_trigger",
        "training_trigger",
        "start_scheduler",
    ],
)
def test_disabled_schedule_never_reads_or_writes_operational_state(monkeypatch, entrypoint):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled scheduling reached operational work")

    for name in (
        "transaction",
        "real_experiments",
        "enqueue",
        "enqueue_missed",
        "ReconciliationWatchdog",
        "now",
    ):
        monkeypatch.setattr(scheduling, name, forbidden)
    if entrypoint == "daily":
        assert scheduling.daily_forecast_decision("experiment", CREATED, TARGET, TARGET) is None
    else:
        assert getattr(scheduling, entrypoint)() is None


@pytest.fixture
def owned_experiments(monkeypatch):
    result = {}
    for provider in ("jma", "usgs", "synthetic"):
        identity = "scheduler_" + uuid4().hex[:12]
        result[provider] = ensure_experiment(
            ExperimentSpec(
                name=identity,
                provider=provider,
                catalogue_namespace=identity if provider == "jma" else None,
                west=140,
                east=141,
                south=35,
                north=36,
            )
        )
    # Scope SQL before the production synthetic filter/limit; other test cases
    # may have created more than the installation's automatic scheduling cap.
    original = scheduling.real_experiments
    ids = {row.id for row in result.values()}

    def selected(*, session=None):
        if session is None:
            with transaction() as owned:
                return selected(session=owned)
        scoped = SimpleNamespace(
            scalars=lambda statement: session.scalars(statement.where(Experiment.id.in_(ids)))
        )
        return original(session=scoped)

    monkeypatch.setattr(scheduling, "real_experiments", selected)
    monkeypatch.setattr(scheduling, "STATE_ID", "test-" + uuid4().hex)
    target, _ = next_day(max(row.created_at for row in result.values()) + timedelta(days=1))
    return SimpleNamespace(experiments=result, target=target)


def experiment_jobs(case):
    ids = [row.id for row in case.experiments.values()]
    with transaction() as session:
        return list(session.scalars(select(Job).where(Job.payload["experiment_id"].as_string().in_(ids))))


def stored_job(identity):
    with transaction() as session:
        return canonical(
            dict(session.execute(select(Job.__table__).where(Job.id == identity)).mappings().one())
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "minutes_before,status", [(21, None), (20, "queued"), (15, "queued"), (1, "queued"), (0, "missed")]
)
def test_startup_boundary_creates_only_valid_daily_work_and_safe_catchup(
    owned_experiments, minutes_before, status
):
    case = owned_experiments
    timestamp = case.target - timedelta(minutes=minutes_before)
    scheduling.reconcile_startup(timestamp.astimezone(JST))
    first = experiment_jobs(case)
    original = {row.id: stored_job(row.id) for row in first}
    scheduling.reconcile_startup(timestamp)
    scheduling.forecast_trigger(timestamp)
    rows = experiment_jobs(case)
    assert {row.id for row in rows} == set(original)
    assert {row.id: stored_job(row.id) for row in rows} == original
    assert not any(row.payload["experiment_id"] == case.experiments["synthetic"].id for row in rows)
    targets = [row for row in rows if row.kind == "forecast" and utc(row.payload["start"]) == case.target]
    assert len(targets) == (0 if status is None else 2)
    assert all(row.status == status for row in targets)
    assert all(
        row.status == "missed"
        for row in rows
        if row.kind == "forecast" and utc(row.payload["start"]) <= timestamp
    )
    assert all(utc(row.payload["start"]) <= case.target for row in rows if row.kind == "forecast")
    ingestions = [row for row in rows if row.kind == "ingest"]
    assert len(ingestions) == 1 and all(
        row.payload["experiment_id"] == case.experiments["usgs"].id for row in ingestions
    )
    assert {utc(row.payload["end"]) for row in ingestions} == {
        timestamp.replace(minute=timestamp.minute // 15 * 15, second=0, microsecond=0)
    }
    assert len([row for row in rows if row.kind == "evaluate"]) == 2
    assert len([row for row in rows if row.kind == "reconcile"]) == 2


@pytest.mark.integration
def test_existing_daily_job_states_are_preserved_byte_for_byte(owned_experiments):
    case = owned_experiments
    experiment = case.experiments["jma"]
    for offset, status in enumerate(("queued", "running", "completed", "failed", "cancelled", "missed")):
        target = case.target + timedelta(days=offset)
        key = f"daily:{experiment.id}:{target.isoformat()}"
        with transaction() as session:
            row = Job(
                kind="forecast",
                logical_key=key,
                payload={
                    "experiment_id": experiment.id,
                    "start": target.isoformat(),
                    "scheduled": True,
                    "original": True,
                },
                status=status,
                attempts=2,
                progress=0.4,
                heartbeat=target - timedelta(minutes=5),
                lease_until=target + timedelta(minutes=1),
                cancel_requested=True,
                error="original diagnostic",
                result={"original": "result"},
            )
            session.add(row)
            session.flush()
        original = stored_job(row.id)
        for timestamp in (target - timedelta(minutes=15), target, target + timedelta(days=1)):
            found = scheduling.daily_forecast_decision(
                experiment.id, experiment.created_at, target, timestamp
            )
            assert found.id == row.id and found.status == status
            assert stored_job(row.id) == original


@pytest.mark.integration
def test_concurrent_startup_and_cron_share_one_upcoming_job(owned_experiments):
    case = owned_experiments
    timestamp = case.target - timedelta(minutes=15)
    barrier = Barrier(3)

    def run(trigger):
        barrier.wait(timeout=10)
        trigger(timestamp)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(run, trigger)
            for trigger in (
                scheduling.reconcile_startup,
                scheduling.forecast_trigger,
                scheduling.reconcile_startup,
            )
        ]
        for future in futures:
            future.result(timeout=20)
    rows = experiment_jobs(case)
    assert len({row.logical_key for row in rows}) == len(rows)
    upcoming = [row for row in rows if row.kind == "forecast" and utc(row.payload["start"]) == case.target]
    assert len(upcoming) == 2 and all(row.status == "queued" for row in upcoming)
    assert len([row for row in rows if row.kind == "ingest"]) == 1
    assert len([row for row in rows if row.kind == "evaluate"]) == 2
    assert len([row for row in rows if row.kind == "reconcile"]) == 2


@pytest.mark.integration
def test_missed_decision_is_terminal_in_its_initial_database_insert():
    token = uuid4().hex
    key, constraint = f"atomic-missed:{token}", f"atomic_missed_{token}"
    # This targeted DB constraint rejects any transient queued row, even if a
    # later transaction would change it to missed. Identifiers are local UUIDs.
    with transaction() as session:
        session.execute(
            text(
                f"ALTER TABLE jobs ADD CONSTRAINT {constraint} CHECK (logical_key <> '{key}' OR status = 'missed')"
            )
        )
    try:
        job = jobs.enqueue_missed(
            "forecast", {"start": TARGET.isoformat()}, key, "Expired before publication"
        )
        assert job.status == "missed" and job.attempts == 0 and job.lease_until is None
        assert job.heartbeat is not None and job.error == "Expired before publication"
        assert jobs.enqueue_missed("forecast", {"changed": True}, key, "different reason").id == job.id
        with transaction() as session:
            row = session.get(Job, job.id)
            assert row.payload == {"start": TARGET.isoformat()} and row.error == "Expired before publication"
    finally:
        with transaction() as session:
            session.execute(text(f"ALTER TABLE jobs DROP CONSTRAINT {constraint}"))


@pytest.mark.integration
def test_missed_insert_waits_for_concurrent_queue_winner_without_overwriting_it():
    key = "queued-winner:" + uuid4().hex
    attempted = ThreadEvent()

    def observe_insert(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO jobs") and parameters.get("logical_key") == key:
            attempted.set()

    event.listen(engine, "before_cursor_execute", observe_insert)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction() as session:
                winner = Job(kind="forecast", logical_key=key, payload={"winner": True}, status="queued")
                session.add(winner)
                session.flush()
                attempted.clear()
                future = pool.submit(jobs.enqueue_missed, "forecast", {"loser": True}, key, "Expired")
                assert attempted.wait(timeout=10)
                assert not future.done()  # Unique-key conflict waits for the uncommitted winner.
            found = future.result(timeout=15)
        assert found.id == winner.id and found.status == "queued" and found.payload == {"winner": True}
        assert found.error is None
    finally:
        event.remove(engine, "before_cursor_execute", observe_insert)
