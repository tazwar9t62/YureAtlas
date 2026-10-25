"""Durable planner acceptance; database cases use only the owned test harness."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select
from yureatlas import scheduling
from yureatlas.artifacts import canonical
from yureatlas.catalogue import ensure_experiment
from yureatlas.db import engine, transaction
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Experiment, Job, SchedulerState
from yureatlas.timeutil import JST, next_day, utc


@pytest.mark.parametrize(
    "timestamp,quarter,evaluation,reconciliation,training",
    [
        (
            "2025-01-01T00:00:00",
            "2025-01-01T00:00:00",
            "2024-12-31T06:00:00",
            "2024-12-31T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-01T05:59:59",
            "2025-01-01T05:45:00",
            "2024-12-31T06:00:00",
            "2024-12-31T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-01T06:00:00",
            "2025-01-01T06:00:00",
            "2025-01-01T06:00:00",
            "2024-12-31T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-01T06:29:59",
            "2025-01-01T06:15:00",
            "2025-01-01T06:00:00",
            "2024-12-31T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-01T06:30:00",
            "2025-01-01T06:30:00",
            "2025-01-01T06:00:00",
            "2025-01-01T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-05T02:59:59",
            "2025-01-05T02:45:00",
            "2025-01-04T06:00:00",
            "2025-01-04T06:30:00",
            "2024-12-29T03:00:00",
        ),
        (
            "2025-01-05T03:00:00",
            "2025-01-05T03:00:00",
            "2025-01-04T06:00:00",
            "2025-01-04T06:30:00",
            "2025-01-05T03:00:00",
        ),
    ],
)
def test_latest_due_uses_exact_jst_and_utc_occurrences(
    timestamp, quarter, evaluation, reconciliation, training
):
    expected = {
        kind: utc(value + "+09:00")
        for kind, value in zip(
            ("ingest", "evaluate", "reconcile", "train"),
            (quarter, evaluation, reconciliation, training),
            strict=True,
        )
    }
    instant = utc(timestamp + "+09:00")
    for timezone in (JST, ZoneInfo("UTC"), ZoneInfo("America/Los_Angeles")):
        assert scheduling.latest_due(instant.astimezone(timezone)) == expected
    assert all(due <= instant for due in expected.values())


def test_naive_reconciliation_time_is_rejected_before_database_access(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
    monkeypatch.setattr(scheduling, "transaction", lambda: pytest.fail("Naive time reached database"))
    with pytest.raises(ValueError, match="timezone"):
        scheduling.reconcile(utc("2025-01-01T00:00:00Z").replace(tzinfo=None))


@pytest.fixture
def planner(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
    state_id = "recovery-test-" + uuid4().hex
    monkeypatch.setattr(scheduling, "STATE_ID", state_id)
    experiments = {}
    for provider in ("jma", "usgs", "synthetic"):
        identity = "recovery_" + uuid4().hex[:12]
        experiments[provider] = ensure_experiment(
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
    ids = {row.id for row in experiments.values()}
    original = scheduling.real_experiments

    def selected(*, session=None):
        if session is None:
            with transaction() as owned:
                return selected(session=owned)
        # Add the owned IDs to SQL before its production LIMIT; retain the
        # actual synthetic exclusion and cap rather than duplicating them.
        scoped = SimpleNamespace(
            scalars=lambda statement: session.scalars(statement.where(Experiment.id.in_(ids)))
        )
        return original(session=scoped)

    monkeypatch.setattr(scheduling, "real_experiments", selected)
    target, _ = next_day(max(row.created_at for row in experiments.values()) + timedelta(days=1))
    return SimpleNamespace(experiments=experiments, ids=ids, state_id=state_id, target=target)


def rows_for(case):
    with transaction() as session:
        return list(
            session.scalars(select(Job).where(Job.payload["experiment_id"].as_string().in_(case.ids)))
        )


def state_for(case):
    with transaction() as session:
        return session.get(SchedulerState, case.state_id)


def stored_rows(case):
    with transaction() as session:
        rows = session.execute(
            select(Job.__table__).where(Job.payload["experiment_id"].as_string().in_(case.ids))
        ).mappings()
        return {row["id"]: canonical(dict(row)) for row in rows}


def stored_state(case):
    with transaction() as session:
        row = (
            session.execute(select(SchedulerState.__table__).where(SchedulerState.id == case.state_id))
            .mappings()
            .one_or_none()
        )
        return canonical(dict(row)) if row else None


@pytest.mark.integration
def test_live_sleep_recovers_upcoming_once_then_preserves_it_at_midnight(planner):
    case = planner
    before = case.target - timedelta(minutes=21)
    scheduling.reconcile(before)
    assert not any(
        row.kind == "forecast" and utc(row.payload["start"]) == case.target for row in rows_for(case)
    )

    waking = case.target - timedelta(minutes=15)
    result = scheduling.reconcile(waking)
    upcoming = [
        row for row in rows_for(case) if row.kind == "forecast" and utc(row.payload["start"]) == case.target
    ]
    assert len(upcoming) == 2 and all(row.status == "queued" for row in upcoming)
    original = stored_rows(case)
    assert result["clock_regressed"] is False
    assert state_for(case).observed_at == waking

    repeated = scheduling.reconcile(waking)
    assert repeated["inserted_jobs"] == 0
    scheduling.reconcile(case.target)
    current = stored_rows(case)
    assert all(current[identity] == value for identity, value in original.items())
    assert state_for(case).reconciled_at == state_for(case).observed_at == case.target
    assert not any(row.payload["experiment_id"] == case.experiments["synthetic"].id for row in rows_for(case))


@pytest.mark.integration
def test_clock_regression_then_sleep_recovers_below_nonrewinding_watermark(planner):
    case = planner
    high = case.target + timedelta(days=60, hours=12)
    initial = scheduling.reconcile(high)
    low = case.target + timedelta(days=1, hours=12)
    regressed = scheduling.reconcile(low)
    assert regressed["clock_regressed"] and regressed["clock_below_watermark"]
    assert state_for(case).reconciled_at == high and state_for(case).observed_at == low
    intermediate = {case.target + timedelta(days=day) for day in (2, 3, 4)}
    assert not any(
        row.kind == "forecast" and utc(row.payload["start"]) in intermediate for row in rows_for(case)
    )

    waking = case.target + timedelta(days=5, hours=12)
    recovered = scheduling.reconcile(waking)
    assert not recovered["clock_regressed"] and recovered["clock_below_watermark"]
    assert state_for(case).reconciled_at == high and state_for(case).observed_at == waking
    missed = [
        row for row in rows_for(case) if row.kind == "forecast" and utc(row.payload["start"]) in intermediate
    ]
    assert len(missed) == 6 and all(row.status == "missed" for row in missed)
    assert recovered["last_truncation"] == initial["last_truncation"]


@pytest.mark.integration
def test_long_downtime_caps_expired_targets_and_keeps_visible_truncation(planner):
    case = planner
    scheduling.reconcile(case.target - timedelta(minutes=21))
    original = stored_rows(case)
    waking = case.target + timedelta(days=100) - timedelta(minutes=15)
    result = scheduling.reconcile(waking)
    added = [row for row in rows_for(case) if row.id not in original]
    forecast = [row for row in added if row.kind == "forecast"]
    today = case.target + timedelta(days=99)
    retained = today - timedelta(days=30)
    expired = [row for row in forecast if row.status == "missed"]
    assert len(expired) == 2 * 31
    assert {utc(row.payload["start"]) for row in expired} == {retained + timedelta(days=n) for n in range(31)}
    upcoming = [row for row in forecast if row.status == "queued"]
    assert len(upcoming) == 2 and {utc(row.payload["start"]) for row in upcoming} == {
        today + timedelta(days=1)
    }
    truncation = result["last_truncation"]
    assert truncation["retained_from"] == retained.isoformat()
    assert truncation["omitted_target_count"] == 2 * 69
    assert {item["experiment_id"] for item in truncation["experiment_intervals"]} == {
        case.experiments["jma"].id,
        case.experiments["usgs"].id,
    }
    assert all(
        item["from"] == case.target.isoformat() and item["target_count"] == 69
        for item in truncation["experiment_intervals"]
    )
    assert scheduling.reconcile(waking + timedelta(minutes=1))["last_truncation"] == truncation
    assert all(stored_rows(case)[identity] == value for identity, value in original.items())

    # A hundred-day absence coalesces old safe occurrences, including training.
    assert {
        kind: len([row for row in added if row.kind == kind])
        for kind in ("ingest", "evaluate", "reconcile", "train")
    } == {"ingest": 1, "evaluate": 2, "reconcile": 2, "train": 2}
    for row in added:
        if row.kind not in {"ingest", "reconcile", "train"}:
            continue
        due = utc(row.payload["scheduled_for"])
        assert utc(row.payload["end"]) == due <= waking
        if row.kind in {"ingest", "reconcile"}:
            assert due - utc(row.payload["start"]) == timedelta(days=2 if row.kind == "ingest" else 31)
        else:
            local = due.astimezone(JST)
            assert local.weekday() == 6 and (local.hour, local.minute, local.second) == (3, 0, 0)
            assert waking - due < timedelta(days=7)


@pytest.mark.integration
def test_early_recovery_does_not_suppress_later_0630_occurrence_or_rewrite_legacy_job(planner):
    case = planner
    before = case.target + timedelta(days=8, hours=6, minutes=20)
    old_due = case.target + timedelta(days=7, hours=6, minutes=30)
    experiment = case.experiments["usgs"]
    legacy_key = f"reconcile:{experiment.id}:{old_due.astimezone(JST).date().isoformat()}"
    with transaction() as session:
        legacy = Job(
            kind="evaluate",
            logical_key=legacy_key,
            payload={"experiment_id": experiment.id, "legacy": True},
            status="completed",
            result={"retained": True},
        )
        session.add(legacy)
        session.flush()
    legacy_bytes = stored_rows(case)[legacy.id]
    scheduling.reconcile(before)
    first = stored_rows(case)
    assert first[legacy.id] == legacy_bytes
    due = case.target + timedelta(days=8, hours=6, minutes=30)
    today_key = f"reconcile:{experiment.id}:{due.astimezone(JST).date().isoformat()}"
    assert not any(row.logical_key == today_key for row in rows_for(case))
    scheduling.reconcile(due)
    new = [row for row in rows_for(case) if row.logical_key == today_key]
    assert len(new) == 1 and new[0].kind == "reconcile"
    assert utc(new[0].payload["end"]) == due
    assert utc(new[0].payload["start"]) == due - timedelta(days=31)
    assert all(stored_rows(case)[identity] == value for identity, value in first.items())


@pytest.mark.integration
@pytest.mark.parametrize("existing_cursor", [False, True])
def test_partial_job_insert_failure_rolls_back_jobs_and_cursor(planner, monkeypatch, existing_cursor):
    case = planner
    if existing_cursor:
        scheduling.reconcile(case.target - timedelta(minutes=21))
    original_rows, original_state = stored_rows(case), stored_state(case)
    original_keys = {row.logical_key for row in rows_for(case)}
    enqueue_batch = scheduling.enqueue_batch
    inserted = []

    def fail_after_insert(plans, *, session):
        new = next(plan for plan in plans if plan["key"] not in original_keys)
        inserted.extend(enqueue_batch([new], session=session))
        assert inserted == [new["key"]]
        assert session.scalar(select(Job).where(Job.logical_key == new["key"])) is not None
        raise RuntimeError("Injected failure after a real job insert")

    monkeypatch.setattr(scheduling, "enqueue_batch", fail_after_insert)
    with pytest.raises(RuntimeError, match="after a real job insert"):
        scheduling.reconcile(case.target - timedelta(minutes=15))
    assert inserted
    assert stored_rows(case) == original_rows and stored_state(case) == original_state
    monkeypatch.setattr(scheduling, "enqueue_batch", enqueue_batch)
    assert scheduling.reconcile(case.target - timedelta(minutes=15))["inserted_jobs"] > 0


@pytest.mark.integration
def test_concurrent_planners_commit_one_decision_set_and_cursor(planner):
    case = planner
    timestamp = case.target - timedelta(minutes=15)
    barrier = Barrier(3)

    def reconcile():
        barrier.wait(timeout=10)
        return scheduling.reconcile(timestamp)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(reconcile) for _ in range(3)]
        results = [future.result(timeout=20) for future in futures]
    rows = rows_for(case)
    assert len({row.logical_key for row in rows}) == len(rows)
    assert sorted(row["inserted_jobs"] for row in results) == [0, 0, len(rows)]
    assert all(
        row["planned_jobs"] == row["inserted_jobs"] + row["existing_jobs"] == len(rows) for row in results
    )
    assert state_for(case).reconciled_at == state_for(case).observed_at == timestamp


@pytest.mark.integration
def test_experiment_cap_is_checked_before_any_jobs_or_cursor_are_written(planner, monkeypatch):
    case = planner
    monkeypatch.setattr(scheduling, "MAX_REAL_EXPERIMENTS", 1)
    with pytest.raises(ValueError, match="at most 1 real experiments"):
        scheduling.reconcile(case.target)
    assert not rows_for(case) and state_for(case) is None
    monkeypatch.setattr(scheduling, "MAX_REAL_EXPERIMENTS", 2)
    assert scheduling.reconcile(case.target)["experiment_count"] == 2
    assert not any(row.payload["experiment_id"] == case.experiments["synthetic"].id for row in rows_for(case))


@pytest.mark.integration
def test_disabled_reconciliation_preserves_existing_jobs_and_durable_cursor(planner, monkeypatch):
    case = planner
    scheduling.reconcile(case.target)
    before = stored_rows(case), stored_state(case)
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", False)
    monkeypatch.setattr(scheduling, "now", lambda: pytest.fail("Disabled planner sampled clock"))
    assert scheduling.reconcile() == {"status": "disabled"}
    assert (stored_rows(case), stored_state(case)) == before


@pytest.mark.integration
def test_default_clock_is_sampled_after_advisory_lock_before_deadline_decision(planner, monkeypatch):
    case = planner
    lock_acquired = []
    sampled = []

    def after_execute(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock(8247714, 1)" in statement:
            lock_acquired.append(True)

    def clock():
        assert lock_acquired == [True], "A pre-lock timestamp could become stale across midnight"
        sampled.append(case.target)
        return case.target

    event.listen(engine, "after_cursor_execute", after_execute)
    monkeypatch.setattr(scheduling, "now", clock)
    try:
        scheduling.reconcile()
    finally:
        event.remove(engine, "after_cursor_execute", after_execute)
    assert sampled == [case.target]
    targets = [
        row for row in rows_for(case) if row.kind == "forecast" and utc(row.payload["start"]) == case.target
    ]
    assert len(targets) == 2 and all(row.status == "missed" for row in targets)
    assert state_for(case).observed_at == case.target
