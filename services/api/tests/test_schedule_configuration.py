"""Requested policy, committed application and queued work agree in owned PostGIS."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from threading import Barrier, Event, current_thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as OrmSession
from yureatlas import api, jobs, scheduling
from yureatlas import schedule_configuration as configuration
from yureatlas.artifacts import canonical
from yureatlas.catalogue import ensure_experiment
from yureatlas.db import engine, transaction
from yureatlas.execution import execution_scope
from yureatlas.schedule_schemas import SCHEDULE_KINDS, ScheduleSpec, ScheduleUpdate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Job, ScheduleConfig, SchedulerRuntime, SchedulerState, WorkerState
from yureatlas.timeutil import JST, next_day, utc
from yureatlas.worker_locks import worker_session


def current_request():
    with transaction() as session:
        return configuration.load_schedule(session)


def change(spec, reason="Owned acceptance policy"):
    return configuration.update_schedule(
        ScheduleUpdate(expected_revision=current_request()["revision"], spec=spec, reason=reason)
    )


@pytest.fixture
def schedule_case(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
    runtime_id = "schedule-test-" + uuid4().hex
    monkeypatch.setattr(scheduling, "STATE_ID", runtime_id)
    monkeypatch.setattr(configuration, "RUNTIME_ID", runtime_id)
    original = current_request()
    experiments = {}
    for name, provider in (("jma", "jma"), ("other", "jma"), ("usgs", "usgs"), ("demo", "synthetic")):
        namespace = "schedule_" + uuid4().hex[:16]
        experiments[name] = ensure_experiment(
            ExperimentSpec(
                name=namespace,
                provider=provider,
                catalogue_namespace=namespace if provider == "jma" else None,
                west=140,
                east=140.5,
                south=35,
                north=35.5,
            )
        )
    ids = {row.id for row in experiments.values()}
    owned_jobs = set()

    def restrict_job_locks(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id.in_(owned_jobs))

    def spec(*names, enabled=True, kinds=SCHEDULE_KINDS):
        return {
            "enabled": enabled,
            "scope": "selected",
            "experiment_ids": [experiments[name].id for name in names],
            "kinds": list(kinds),
        }

    def add_job(kind, name="jma", *, scheduled=True):
        payload = {"experiment_id": experiments[name].id}
        if scheduled:
            payload["scheduled"] = True
        with transaction() as session:
            row = Job(kind=kind, logical_key="schedule-job:" + uuid4().hex, payload=payload)
            session.add(row)
            session.flush()
            owned_jobs.add(row.id)
            return row

    change(spec("jma", "usgs"))
    event.listen(OrmSession, "do_orm_execute", restrict_job_locks)
    try:
        yield SimpleNamespace(
            experiments=experiments,
            ids=ids,
            runtime_id=runtime_id,
            spec=spec,
            add_job=add_job,
            owned_jobs=owned_jobs,
        )
    finally:
        event.remove(OrmSession, "do_orm_execute", restrict_job_locks)
        # Configuration history is immutable. Restore the prior request through
        # another audited revision rather than deleting installation history.
        change(original["spec"], reason="Restore policy after owned schedule acceptance")
        with transaction() as session:
            session.execute(
                update(Job)
                .where(
                    Job.payload["experiment_id"].as_string().in_(ids),
                    Job.status.in_(["queued", "running"]),
                )
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )


def table_row(table, identity):
    with transaction() as session:
        row = session.execute(select(table.__table__).where(table.id == identity)).mappings().one_or_none()
        return canonical(dict(row)) if row else None


def scheduled_rows(case):
    with transaction() as session:
        return list(
            session.scalars(select(Job).where(Job.payload["experiment_id"].as_string().in_(case.ids)))
        )


def database_now():
    with transaction() as session:
        return session.scalar(text("SELECT clock_timestamp()"))


def test_empty_installation_has_virtual_defaults_without_creating_a_row():
    request = configuration.load_schedule(SimpleNamespace(scalar=lambda statement: None))
    assert request["revision"] == 0 and request["id"] is None and request["created_at"] is None
    assert request["spec"] == {
        "enabled": True,
        "scope": "all_real",
        "experiment_ids": [],
        "kinds": list(SCHEDULE_KINDS),
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_revision": -1},
        {"reason": " \n\t "},
        {"reason": 42},
        {"reason": "x" * 501},
        {"unexpected": "ignored?"},
        {"spec": {"enabled": "false"}},
        {"spec": {"enabled": 0}},
        {"spec": {"scope": "demo"}},
        {"spec": {"experiment_ids": ["real"]}},
        {"spec": {"scope": "selected", "experiment_ids": ["same", "same"]}},
        {"spec": {"scope": "selected", "experiment_ids": [" padded "]}},
        {"spec": {"scope": "selected", "experiment_ids": [1]}},
        {"spec": {"scope": "selected", "experiment_ids": [str(i) for i in range(101)]}},
        {"spec": {"kinds": ["forecast", "forecast"]}},
        {"spec": {"kinds": ["promote"]}},
        {"spec": {"cron": "* * * * *"}},
    ],
)
def test_invalid_schedule_update_is_rejected_before_database(monkeypatch, patch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid schedule request reached persistence")

    monkeypatch.setattr(configuration, "transaction", forbidden)
    with TestClient(api.app, headers={"X-YureAtlas-Request": "1"}) as client:
        response = client.put(
            "/schedules", json={"expected_revision": 0, "spec": {}, "reason": "valid", **patch}
        )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("parameters", [{"limit": 0}, {"limit": 101}, {"before_revision": 0}])
def test_invalid_history_bounds_are_rejected_before_database(monkeypatch, parameters):
    monkeypatch.setattr(configuration, "transaction", lambda: pytest.fail("Invalid history reached database"))
    with TestClient(api.app, headers={"X-YureAtlas-Request": "1"}) as client:
        response = client.get("/schedules/history", params=parameters)
    assert response.status_code == 422, response.text


def test_normalization_preserves_valid_empty_no_work_policies():
    body = ScheduleUpdate(
        expected_revision=0,
        reason="  operator reason\n",
        spec={"scope": "selected", "experiment_ids": ["z", "a"], "kinds": ["train", "forecast"]},
    )
    assert body.reason == "operator reason"
    assert body.spec.experiment_ids == ["a", "z"] and body.spec.kinds == ["forecast", "train"]
    assert ScheduleSpec(scope="selected", experiment_ids=[], kinds=[]).model_dump()["kinds"] == []


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2025-01-04T23:39:59+09:00", "2025-01-04T23:40:00+09:00"),
        ("2025-01-04T23:40:00+09:00", "2025-01-05T23:40:00+09:00"),
        ("2025-01-05T00:00:00+09:00", "2025-01-05T23:40:00+09:00"),
    ],
)
def test_regular_occurrences_are_strictly_future_jst_cadence(timestamp, expected):
    instant = utc(timestamp)
    result = scheduling.next_regular_due(instant)
    assert result["forecast"] == utc(expected)
    assert all(value > instant for value in result.values())
    assert scheduling.next_regular_due(instant.astimezone(ZoneInfo("America/Los_Angeles"))) == result
    train = result["train"].astimezone(JST)
    assert train.weekday() == 6 and (train.hour, train.minute) == (3, 0)


@pytest.mark.integration
def test_api_persists_normalized_policy_and_audit_across_client_restart(schedule_case):
    case = schedule_case
    before = current_request()
    spec = case.spec("usgs", "jma", kinds=("train", "forecast"))
    with TestClient(api.app, headers={"X-YureAtlas-Request": "1"}) as client:
        response = client.put(
            "/schedules",
            json={"expected_revision": before["revision"], "spec": spec, "reason": "  reason one  "},
        )
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["revision"] == before["revision"] + 1 and saved["reason"] == "reason one"
        assert saved["spec"]["experiment_ids"] == sorted(spec["experiment_ids"])
        assert saved["spec"]["kinds"] == ["forecast", "train"]
        conflict = client.put(
            "/schedules", json={"expected_revision": before["revision"], "spec": spec, "reason": "stale"}
        )
        assert conflict.status_code == 409
    with TestClient(api.app, headers={"X-YureAtlas-Request": "1"}) as restarted:
        status = restarted.get("/schedules")
        assert status.status_code == 200, status.text
        assert status.json()["requested"] == saved
        history = restarted.get("/schedules/history", params={"limit": 1}).json()
        assert history["items"] == [saved] and history["has_more"]
        older = restarted.get(
            "/schedules/history", params={"limit": 1, "before_revision": history["next_before_revision"]}
        ).json()
        assert older["items"][0]["revision"] == before["revision"]
    assert current_request()["revision"] == saved["revision"]


@pytest.mark.integration
def test_selected_scope_rejects_unknown_and_synthetic_without_new_revision(schedule_case):
    original = current_request()
    for identity in (uuid4().hex, schedule_case.experiments["demo"].id):
        with TestClient(api.app, headers={"X-YureAtlas-Request": "1"}) as client:
            response = client.put(
                "/schedules",
                json={
                    "expected_revision": original["revision"],
                    "spec": {"scope": "selected", "experiment_ids": [identity]},
                    "reason": "Invalid scope",
                },
            )
        assert response.status_code == 422, response.text
        assert current_request() == original


@pytest.mark.integration
def test_concurrent_compare_and_swap_has_one_winner_and_immutable_history(schedule_case):
    original = current_request()
    barrier = Barrier(2)

    def save(name):
        barrier.wait(timeout=10)
        try:
            return configuration.update_schedule(
                ScheduleUpdate(
                    expected_revision=original["revision"], spec=schedule_case.spec(name), reason=name
                )
            )
        except configuration.ScheduleConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=15) for future in [pool.submit(save, "jma"), pool.submit(save, "usgs")]
        ]
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1 and sum(isinstance(r, configuration.ScheduleConflict) for r in results) == 1
    winner = winners[0]
    assert winner["revision"] == original["revision"] + 1 and current_request() == winner
    prior = table_row(ScheduleConfig, winner["id"])
    for statement in (
        update(ScheduleConfig).where(ScheduleConfig.id == winner["id"]).values(reason="rewritten"),
        delete(ScheduleConfig).where(ScheduleConfig.id == winner["id"]),
    ):
        with pytest.raises(DBAPIError, match="[Ii]mmutable|[Aa]ppend"), transaction() as session:
            session.execute(statement)
        assert table_row(ScheduleConfig, winner["id"]) == prior


@pytest.mark.integration
def test_application_requires_current_live_worker_and_exact_revision(schedule_case):
    case = schedule_case
    with worker_session() as owner:
        before = configuration.get_schedule_status()
        assert before.worker.status == "fresh" and before.effective_enabled is True
        assert before.application_status == "pending" and before.last_application is None
        with execution_scope(owner):
            scheduling.reconcile()
        applied = configuration.get_schedule_status()
        assert applied.application_status == "current"
        assert applied.last_application.owner_token == owner.token
        assert applied.last_application.revision == applied.requested.revision
        assert {row.kind for row in applied.next_regular_runs} == set(SCHEDULE_KINDS)
        assert all(
            row.at_utc > applied.checked_at and "+09:00" in row.at_jst for row in applied.next_regular_runs
        )
        forecast = next(row for row in applied.next_regular_runs if row.kind == "forecast")
        assert forecast.forecast_target_start - forecast.at_utc == timedelta(minutes=20)
        assert (
            applied.checked_at
            <= applied.recovery.next_check_estimate
            <= applied.checked_at + timedelta(seconds=30)
        )
        change(case.spec("jma", "usgs"), reason="Reason only still needs a worker acknowledgment")
        pending = configuration.get_schedule_status()
        assert (
            pending.application_status == "pending"
            and pending.last_application.revision == applied.requested.revision
        )
        with execution_scope(owner):
            scheduling.reconcile()
        assert configuration.get_schedule_status().application_status == "current"
    offline = configuration.get_schedule_status()
    assert offline.worker.status == "offline" and offline.application_status == "worker_unverified"
    assert offline.effective_enabled is None and offline.next_regular_runs == []
    with worker_session() as successor:
        pending = configuration.get_schedule_status()
        assert pending.worker.owner_token == successor.token and pending.application_status == "pending"
        assert pending.last_application.owner_token == owner.token
        with execution_scope(successor):
            scheduling.reconcile()
        assert configuration.get_schedule_status().application_status == "current"


@pytest.mark.integration
def test_process_gate_is_observed_from_worker_and_unknown_gate_is_not_applied(schedule_case, monkeypatch):
    case = schedule_case
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", False)
    with worker_session():
        # The API and worker may load different process environments. Keep the
        # registered worker value false while observing the API's true value.
        monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
        status = configuration.get_schedule_status()
        assert status.api_environment_enabled is True and status.worker.environment_enabled is False
        assert status.effective_enabled is False and status.application_status == "environment_disabled"
        assert status.next_regular_runs == [] and status.recovery.next_check_estimate is None
    with worker_session() as owner:
        change(case.spec("jma", enabled=False))
        with execution_scope(owner):
            scheduling.reconcile()
        assert configuration.get_schedule_status().application_status == "current"
        with transaction() as session:
            worker = session.get(WorkerState, "scheduler")
            worker.details = {
                key: value for key, value in worker.details.items() if key != "schedule_environment_enabled"
            }
        unknown = configuration.get_schedule_status()
        assert (
            unknown.worker.environment_enabled is None and unknown.application_status == "worker_unverified"
        )


@pytest.mark.integration
def test_stale_future_heartbeat_and_clock_jump_never_claim_current_schedule(schedule_case):
    with worker_session() as owner:
        with execution_scope(owner):
            scheduling.reconcile()
        for offset, expected in ((timedelta(seconds=-91), "stale"), (timedelta(seconds=60), "clock_skew")):
            with transaction() as session:
                session.get(WorkerState, "scheduler").heartbeat = database_now() + offset
            status = configuration.get_schedule_status()
            assert status.worker.status == expected and status.application_status == "worker_unverified"
            assert status.effective_enabled is None and not status.next_regular_runs
        jobs.worker_heartbeat(owner)
        with transaction() as session:
            session.get(SchedulerRuntime, schedule_case.runtime_id).observed_at = database_now() + timedelta(
                days=1000
            )
        status = configuration.get_schedule_status()
        assert (
            status.checked_at
            <= status.recovery.next_check_estimate
            <= status.checked_at + timedelta(seconds=30)
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "names,kinds,enabled,reason",
    [
        (("jma",), ("forecast",), False, "persisted_disabled"),
        ((), ("forecast",), True, "empty_scope"),
        (("jma",), (), True, "empty_kinds"),
        (("jma",), ("ingest",), True, "no_applicable_kinds"),
    ],
)
def test_no_work_policy_acknowledges_without_advancing_cursor(schedule_case, names, kinds, enabled, reason):
    case = schedule_case
    with worker_session() as owner:
        with execution_scope(owner):
            scheduling.reconcile()
        original_cursor = table_row(SchedulerState, case.runtime_id)
        original_jobs = {row.id: table_row(Job, row.id) for row in scheduled_rows(case)}
        request = change(case.spec(*names, kinds=kinds, enabled=enabled))
        with execution_scope(owner):
            result = scheduling.reconcile(database_now() + timedelta(days=4))
        assert result["status"] == "disabled" and result["disable_reason"] == reason
        assert table_row(SchedulerState, case.runtime_id) == original_cursor
        assert {row.id: table_row(Job, row.id) for row in scheduled_rows(case)} == original_jobs
        status = configuration.get_schedule_status()
        assert status.application_status == "current" and status.effective_enabled is False
        assert status.last_application.revision == request["revision"] and not status.next_regular_runs


@pytest.mark.integration
def test_new_scope_and_kind_recover_bounded_history_while_reason_only_preserves_cursor(schedule_case):
    case = schedule_case
    first, _ = next_day(max(row.created_at for row in case.experiments.values()))
    high = first + timedelta(days=50, hours=12)
    change(case.spec("jma", kinds=("evaluate",)))
    scheduling.reconcile(high)
    assert not any(row.kind == "forecast" for row in scheduled_rows(case))
    change(case.spec("jma", "other", kinds=("forecast",)))
    recovered = scheduling.reconcile(high)
    assert recovered["scope_changed"] is True
    missed = [row for row in scheduled_rows(case) if row.kind == "forecast"]
    expected = {first + timedelta(days=n) for n in range(20, 51)}
    for name in ("jma", "other"):
        rows = [row for row in missed if row.payload["experiment_id"] == case.experiments[name].id]
        assert {utc(row.payload["start"]) for row in rows} == expected
        assert len(rows) == 31 and all(row.status == "missed" for row in rows)
    original = {row.id: table_row(Job, row.id) for row in scheduled_rows(case)}
    change(case.spec("other", "jma", kinds=("forecast",)), reason="Same policy; improved rationale")
    repeated = scheduling.reconcile(high + timedelta(minutes=1))
    assert repeated["scope_changed"] is False and repeated["inserted_jobs"] == 0
    assert {row.id: table_row(Job, row.id) for row in scheduled_rows(case)} == original


@pytest.mark.integration
def test_named_automatic_triggers_honor_policy_without_acknowledging_full_application(schedule_case):
    case = schedule_case
    change(case.spec("jma", kinds=("evaluate",)))
    day, _ = next_day(case.experiments["jma"].created_at)
    timestamp = day + timedelta(hours=23, minutes=45)
    triggers = (
        scheduling.ingest_trigger,
        scheduling.forecast_trigger,
        scheduling.evaluate_trigger,
        partial(scheduling.evaluate_trigger, reconcile=True),
        scheduling.training_trigger,
    )
    for trigger in triggers:
        trigger(timestamp=timestamp)
    rows = scheduled_rows(case)
    assert len(rows) == 1 and rows[0].kind == "evaluate"
    assert rows[0].payload["experiment_id"] == case.experiments["jma"].id
    assert table_row(SchedulerRuntime, case.runtime_id) is None
    assert table_row(SchedulerState, case.runtime_id) is None
    original = table_row(Job, rows[0].id)
    change(case.spec("jma", enabled=False))
    for trigger in triggers:
        trigger(timestamp=timestamp + timedelta(days=1))
    assert len(scheduled_rows(case)) == 1 and table_row(Job, rows[0].id) == original


@pytest.mark.integration
def test_claim_pauses_only_disallowed_scheduled_work_and_keeps_running_attempts(schedule_case, monkeypatch):
    case = schedule_case
    change(case.spec("jma", kinds=("evaluate",)))
    paused_kind = case.add_job("train")
    paused_scope = case.add_job("evaluate", "usgs")
    paused_synthetic = case.add_job("evaluate", "demo")
    running = case.add_job("evaluate")
    manual = case.add_job("train", "demo", scheduled=False)
    paused_ids = {paused_kind.id, paused_scope.id, paused_synthetic.id}
    original = {identity: table_row(Job, identity) for identity in paused_ids}
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        assert attempt.id == running.id
        change(case.spec("jma", enabled=False))
        jobs.checkpoint(attempt.id, attempt.attempt_token, owner, 0.5)
        jobs.finish(attempt.id, attempt.attempt_token, owner, "completed", result={"already_running": True})
        next_attempt = jobs.claim(owner)
        assert next_attempt.id == manual.id
        jobs.finish(next_attempt.id, next_attempt.attempt_token, owner, "completed")
        assert jobs.claim(owner) is None
        assert {identity: table_row(Job, identity) for identity in paused_ids} == original
        change(case.spec("jma", kinds=("train",)))
        monkeypatch.setattr(scheduling.settings, "schedule_enabled", False)
        assert jobs.claim(owner) is None
        assert table_row(Job, paused_kind.id) == original[paused_kind.id]
        monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
        resumed = jobs.claim(owner)
        assert resumed.id == paused_kind.id and resumed.attempts == 1
        jobs.finish(resumed.id, resumed.attempt_token, owner, "completed")


@pytest.mark.integration
@pytest.mark.parametrize("blocked_by", ["disabled", "scope", "kind", "environment", "retry_exhausted"])
def test_policy_pauses_dispatch_but_not_expired_terminal_cleanup(schedule_case, monkeypatch, blocked_by):
    case = schedule_case
    change(case.spec("jma", kinds=("evaluate",)))
    row = case.add_job("evaluate")
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        jobs.checkpoint(attempt.id, attempt.attempt_token, owner, 0.4)
        with transaction() as session:
            job = session.get(Job, row.id)
            job.lease_until = database_now() - timedelta(seconds=1)
            if blocked_by == "retry_exhausted":
                job.attempts = jobs.MAX_ATTEMPTS
        if blocked_by != "retry_exhausted":
            assert jobs.request_cancel(row.id).status == "running"
        if blocked_by in {"disabled", "retry_exhausted"}:
            change(case.spec("jma", enabled=False))
        elif blocked_by == "scope":
            change(case.spec("usgs", kinds=("evaluate",)))
        elif blocked_by == "kind":
            change(case.spec("jma", kinds=("train",)))
        else:
            monkeypatch.setattr(scheduling.settings, "schedule_enabled", False)
        # No new dispatch is returned: only the already cancelled/exhausted
        # attempt is terminalized, keeping its attempt identity and progress.
        assert jobs.claim(owner) is None
        with transaction() as session:
            terminal = session.get(Job, row.id)
            assert terminal.status == ("failed" if blocked_by == "retry_exhausted" else "cancelled")
            assert terminal.progress == 0.4 and terminal.lease_until is None
            assert terminal.attempt_token == attempt.attempt_token
            assert terminal.attempts == (jobs.MAX_ATTEMPTS if blocked_by == "retry_exhausted" else 1)


@pytest.mark.integration
def test_over_cap_policy_pauses_scheduled_queue_but_allows_manual_and_disable(schedule_case, monkeypatch):
    case = schedule_case
    # Exercise the actual SQL cap against several real rows with a smaller
    # configured bound; no large fixture population or fake scope rows needed.
    monkeypatch.setattr(configuration, "MAX_EXPERIMENTS", 1)
    monkeypatch.setattr(scheduling, "MAX_REAL_EXPERIMENTS", 1)
    saved = change({"enabled": True, "scope": "all_real", "kinds": ["evaluate"]})
    queued = case.add_job("evaluate")
    manual = case.add_job("train", "demo", scheduled=False)
    original = table_row(Job, queued.id)
    with worker_session() as owner:
        status = configuration.get_schedule_status()
        assert status.requested.revision == saved["revision"]
        assert status.scope_complete is False and status.scope_count == 2
        assert status.effective_enabled is False and not status.next_regular_runs
        attempt = jobs.claim(owner)
        assert attempt.id == manual.id
        jobs.finish(attempt.id, attempt.attempt_token, owner, "completed")
        assert jobs.claim(owner) is None and table_row(Job, queued.id) == original
        with pytest.raises(ValueError, match="at most 1"), execution_scope(owner):
            scheduling.reconcile()
        assert table_row(SchedulerState, case.runtime_id) is None
        assert table_row(SchedulerRuntime, case.runtime_id) is None
        change({"enabled": False, "scope": "all_real", "kinds": ["evaluate"]})
        with execution_scope(owner):
            result = scheduling.reconcile()
        assert result["disable_reason"] == "persisted_disabled"
        assert configuration.get_schedule_status().application_status == "current"
        assert table_row(Job, queued.id) == original


@pytest.mark.integration
def test_jma_only_ingest_is_not_effective_or_claimable(schedule_case):
    case = schedule_case
    change(case.spec("jma", kinds=("ingest",)))
    queued = case.add_job("ingest")
    original = table_row(Job, queued.id)
    with worker_session() as owner:
        with execution_scope(owner):
            result = scheduling.reconcile()
        assert result["disable_reason"] == "no_applicable_kinds"
        status = configuration.get_schedule_status()
        assert status.effective_enabled is False and status.applicable_kinds == []
        assert status.application_status == "current" and not status.next_regular_runs
        assert jobs.claim(owner) is None and table_row(Job, queued.id) == original


@pytest.mark.integration
def test_planning_and_policy_edit_serialize_and_failed_plan_keeps_prior_ack(schedule_case, monkeypatch):
    case = schedule_case
    change(case.spec("jma", kinds=("evaluate",)))
    first, _ = next_day(case.experiments["jma"].created_at)
    previous = current_request()
    staged, release, editing = Event(), Event(), Event()
    editor_pid = []
    original_lock = configuration.lock_schedule

    def locked_editor(session):
        editor_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
        editing.set()
        original_lock(session)

    def hold_insert(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("schedule-planner") and statement.startswith("INSERT INTO jobs"):
            staged.set()
            assert release.wait(timeout=10), "Planner barrier was not released"

    monkeypatch.setattr(configuration, "lock_schedule", locked_editor)
    event.listen(engine, "after_cursor_execute", hold_insert)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="schedule-planner") as planner_pool,
            ThreadPoolExecutor(max_workers=1) as editor_pool,
        ):
            plan = planner_pool.submit(scheduling.reconcile, first + timedelta(hours=12))
            try:
                assert staged.wait(timeout=10)
                edit = editor_pool.submit(change, case.spec("jma", enabled=False))
                assert editing.wait(timeout=10)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    with transaction() as session:
                        blockers = session.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": editor_pid[0]}
                        )
                    if blockers:
                        break
                    sleep(0.01)
                assert blockers and not edit.done(), "Configuration edit did not wait for planner commit"
            finally:
                release.set()
            result = plan.result(timeout=10)
            saved = edit.result(timeout=10)
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", hold_insert)
    assert result["revision"] == previous["revision"] and saved["revision"] == previous["revision"] + 1
    with transaction() as session:
        assert session.get(SchedulerRuntime, case.runtime_id).revision == previous["revision"]
    prior_cursor = table_row(SchedulerState, case.runtime_id)
    prior_runtime = table_row(SchedulerRuntime, case.runtime_id)
    prior_jobs = {row.id: table_row(Job, row.id) for row in scheduled_rows(case)}
    change(case.spec("jma", kinds=("evaluate",)))
    original_enqueue = scheduling.enqueue_batch

    def fail_after_insert(plans, *, session):
        assert original_enqueue(plans, session=session)
        raise RuntimeError("Injected failure after a real job insert")

    monkeypatch.setattr(scheduling, "enqueue_batch", fail_after_insert)
    with pytest.raises(RuntimeError, match="after a real job insert"):
        scheduling.reconcile(first + timedelta(days=1, hours=12))
    assert table_row(SchedulerState, case.runtime_id) == prior_cursor
    assert table_row(SchedulerRuntime, case.runtime_id) == prior_runtime
    assert {row.id: table_row(Job, row.id) for row in scheduled_rows(case)} == prior_jobs
