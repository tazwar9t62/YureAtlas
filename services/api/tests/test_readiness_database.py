"""Bounded diagnostic connections and independently classified worker state."""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from yureatlas import readiness_database as readiness
from yureatlas.catalogue import ensure_experiment
from yureatlas.db import transaction
from yureatlas.schedule_configuration import load_schedule, schedule_spec_hash, update_schedule
from yureatlas.schedule_schemas import ScheduleUpdate, ScheduleWorker
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import SchedulerRuntime
from yureatlas.worker_locks import worker_session

CHECKED_AT = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)


def test_supported_schema_is_the_installed_alembic_head():
    repository = Path(__file__).resolve().parents[3]
    config = Config(str(repository / "alembic.ini"))
    config.set_main_option("script_location", str(repository / "services/api/migrations"))
    config.set_main_option("path_separator", "os")
    assert ScriptDirectory.from_config(config).get_heads() == [readiness.EXPECTED_SCHEMA_REVISION]


@pytest.mark.parametrize("observed", [[], ["009"], ["011"], ["010", "other"], ["010"]])
def test_schema_requires_one_exact_revision_and_bounds_returned_rows(observed):
    queries = []

    def scalars(query):
        queries.append(str(query))
        return observed

    session = SimpleNamespace(scalar=lambda query: "alembic_version", scalars=scalars)
    status = readiness.schema_status(session)
    assert status["status"] == ("ok" if observed == ["010"] else "failed")
    assert status["observed"] == observed and status["expected"] == "010"
    assert len(queries) == 1 and "LIMIT 2" in queries[0]


def test_missing_schema_does_not_query_an_absent_relation():
    session = SimpleNamespace(
        scalar=lambda query: None,
        scalars=lambda query: pytest.fail("Missing schema must not query its absent table"),
    )
    result = readiness.schema_status(session)
    assert result["status"] == "failed" and result["code"] == "schema_missing"


@pytest.mark.parametrize("failure", ["connect", "probe"])
def test_connection_has_independent_startup_limits_and_disposes_on_failure(monkeypatch, failure):
    options = {}
    disposal = []
    queries = []

    @contextmanager
    def connection():
        if failure == "connect":
            raise RuntimeError("connection unavailable")
        yield object()

    @contextmanager
    def session_context(**kwargs):
        @contextmanager
        def begin():
            yield

        yield SimpleNamespace(begin=begin, execute=lambda query: queries.append(str(query)))

    engine = SimpleNamespace(connect=connection, dispose=lambda: disposal.append(True))

    def factory(url, **kwargs):
        options.update(kwargs)
        return engine

    monkeypatch.setattr(readiness, "create_engine", factory)
    monkeypatch.setattr(readiness, "Session", session_context)
    with pytest.raises(RuntimeError, match="unavailable"), readiness.readiness_session():
        raise RuntimeError("probe unavailable")
    assert options["poolclass"] is NullPool and options["pool_pre_ping"] is False
    assert options["connect_args"] == {
        "connect_timeout": 3,
        "options": "-c statement_timeout=2000 -c lock_timeout=1000 -c transaction_timeout=5000",
    }
    assert disposal == [True]
    assert queries == ([] if failure == "connect" else ["SET TRANSACTION READ ONLY"])


@pytest.fixture
def operational(monkeypatch):
    requested = {
        "revision": 4,
        "spec": {"enabled": True, "scope": "selected", "experiment_ids": [], "kinds": []},
    }
    runtime = SimpleNamespace(
        revision=4,
        owner_token="current-worker",
        observed_at=CHECKED_AT,
        status="disabled",
        effective_enabled=False,
        details={"spec_hash": schedule_spec_hash(requested["spec"])},
    )
    worker = ScheduleWorker(
        status="fresh",
        heartbeat=CHECKED_AT,
        owner_token="current-worker",
        environment_enabled=True,
        reason="fixture",
    )
    case = SimpleNamespace(
        requested=requested,
        runtime=runtime,
        worker=worker,
        experiments=[SimpleNamespace(config={"provider": "jma"})],
        complete=True,
    )
    monkeypatch.setattr(readiness, "get_worker_status", lambda *_: case.worker)
    monkeypatch.setattr(readiness, "load_schedule", lambda *_: case.requested)
    monkeypatch.setattr(readiness, "_scope", lambda *_: (case.experiments, case.complete))
    case.session = SimpleNamespace(get=lambda *_: case.runtime)
    return case


@pytest.mark.parametrize("status", ["missing", "unregistered", "offline", "stale", "clock_skew"])
def test_noncurrent_worker_cannot_make_old_application_ready(operational, status):
    case = operational
    case.worker = case.worker.model_copy(update={"status": status})
    result = readiness.operational_status(case.session, CHECKED_AT)
    assert result["worker"].status == status
    assert result["scheduler"]["status"] == "failed"
    assert result["scheduler"]["code"] == "scheduler_worker_unverified"


@pytest.mark.parametrize("gate,expected", [(False, "ok"), (None, "failed")])
def test_environment_disable_is_intentional_but_unknown_gate_is_unverified(operational, gate, expected):
    case = operational
    case.runtime = None
    case.worker = case.worker.model_copy(update={"environment_enabled": gate})
    result = readiness.operational_status(case.session, CHECKED_AT)["scheduler"]
    assert result["status"] == expected
    assert result["code"] == (
        "scheduler_environment_disabled" if gate is False else "scheduler_environment_unknown"
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "scheduler_not_applied"),
        ("owner", "scheduler_owner_mismatch"),
        ("revision", "scheduler_configuration_pending"),
        ("spec", "scheduler_configuration_pending"),
        ("inconsistent", "scheduler_application_invalid"),
        ("stale", "scheduler_application_stale"),
        ("future", "scheduler_clock_skew"),
    ],
)
def test_no_work_policy_still_requires_recent_exact_worker_application(operational, change, code):
    case = operational
    if change == "missing":
        case.runtime = None
    elif change == "owner":
        case.runtime.owner_token = "previous-worker"
    elif change == "revision":
        case.runtime.revision -= 1
    elif change == "spec":
        case.runtime.details = {"spec_hash": "old-spec"}
    elif change == "inconsistent":
        case.runtime.status = "applied"
    else:
        case.runtime.observed_at = CHECKED_AT + timedelta(seconds=-91 if change == "stale" else 6)
    result = readiness.operational_status(case.session, CHECKED_AT)["scheduler"]
    assert result["status"] == "failed" and result["code"] == code


@pytest.mark.parametrize("age", [-5, 0, 90])
def test_disabled_policy_application_accepts_exact_time_tolerances(operational, age):
    case = operational
    case.runtime.observed_at = CHECKED_AT - timedelta(seconds=age)
    result = readiness.operational_status(case.session, CHECKED_AT)["scheduler"]
    assert result["status"] == "ok" and result["code"] == "scheduler_policy_disabled"
    case.runtime.status, case.runtime.effective_enabled = "applied", True
    case.requested["spec"] = {"enabled": True, "scope": "all_real", "kinds": ["evaluate"]}
    case.runtime.details = {"spec_hash": schedule_spec_hash(case.requested["spec"])}
    assert readiness.operational_status(case.session, CHECKED_AT)["scheduler"]["code"] == "scheduler_current"


@pytest.mark.parametrize("scope", ["over_cap", "empty", "jma_ingest", "new_real_experiment"])
def test_scope_changes_cannot_leave_a_recent_application_falsely_ready(operational, scope):
    case = operational
    case.requested["spec"] = {"enabled": True, "scope": "all_real", "kinds": ["evaluate"]}
    case.runtime.status, case.runtime.effective_enabled = "applied", True
    if scope == "over_cap":
        case.complete = False
    elif scope == "empty":
        case.experiments = []
    elif scope == "jma_ingest":
        case.requested["spec"]["kinds"] = ["ingest"]
    else:
        case.runtime.status, case.runtime.effective_enabled = "disabled", False
    case.runtime.details = {"spec_hash": schedule_spec_hash(case.requested["spec"])}
    result = readiness.operational_status(case.session, CHECKED_AT)["scheduler"]
    assert result["status"] == "failed"
    assert result["code"] == (
        "scheduler_scope_exceeded" if scope == "over_cap" else "scheduler_scope_pending"
    )


@pytest.mark.parametrize("disabled_by", ["environment", "persisted", "kinds", "scope"])
def test_explicit_no_work_policy_needs_no_experiment_scan(operational, monkeypatch, disabled_by):
    case = operational
    case.requested["spec"] = {
        "enabled": True,
        "scope": "all_real",
        "kinds": ["evaluate"],
        "experiment_ids": [],
    }
    if disabled_by == "environment":
        case.worker = case.worker.model_copy(update={"environment_enabled": False})
    elif disabled_by == "persisted":
        case.requested["spec"]["enabled"] = False
    elif disabled_by == "kinds":
        case.requested["spec"]["kinds"] = []
    else:
        case.requested["spec"]["scope"] = "selected"
    case.runtime.details = {"spec_hash": schedule_spec_hash(case.requested["spec"])}
    monkeypatch.setattr(readiness, "_scope", lambda *_: pytest.fail("Disabled policy scanned experiments"))
    assert readiness.operational_status(case.session, CHECKED_AT)["scheduler"]["status"] == "ok"


def assert_backend_closed(pid):
    deadline = monotonic() + 3
    while True:
        with transaction() as observer:
            retained = observer.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"), {"pid": pid}
            )
        if not retained or monotonic() >= deadline:
            break
        sleep(0.01)
    assert not retained, "The diagnostic connection was retained instead of physically closed"


@pytest.mark.integration
@pytest.mark.parametrize("error", [False, True])
def test_diagnostic_connection_is_read_only_time_bounded_and_physically_closed(error):
    pid = None
    try:
        failure_context = pytest.raises(DBAPIError) if error else nullcontext()
        with failure_context as failure, readiness.readiness_session() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            assert readiness.schema_status(session)["status"] == "ok"
            values = session.execute(
                text("""SELECT current_setting('transaction_read_only'), current_setting('statement_timeout'),
                    current_setting('lock_timeout'), current_setting('transaction_timeout')""")
            ).one()
            assert tuple(values) == ("on", "2s", "1s", "5s")
            if error:
                # A real write attempt fails even though it would affect no
                # research row. The diagnostic role is enforced by PostgreSQL.
                session.execute(text("UPDATE worker_state SET heartbeat = heartbeat WHERE false"))
        if error:
            assert failure.value.orig.sqlstate == "25006"
    finally:
        if pid is not None:
            assert_backend_closed(pid)


@pytest.mark.integration
def test_actual_statement_timeout_interrupts_a_long_probe_query():
    pid = None
    try:
        with pytest.raises(DBAPIError) as failure, readiness.readiness_session() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            session.execute(text("SELECT pg_sleep(3)"))
        assert failure.value.orig.sqlstate == "57014"  # query_canceled by statement_timeout
    finally:
        if pid is not None:
            assert_backend_closed(pid)


@pytest.mark.integration
def test_actual_transaction_budget_disconnects_several_individually_short_queries():
    pid = None
    completed = 0
    try:
        with pytest.raises(DBAPIError) as failure, readiness.readiness_session() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            # Each query fits the two-second statement limit. Their combined
            # time cannot extend this one diagnostic transaction indefinitely.
            for _ in range(6):
                session.execute(text("SELECT pg_sleep(1.1)"))
                completed += 1
        assert failure.value.orig.sqlstate == "25P04"  # PostgreSQL 17 transaction_timeout
        assert 1 <= completed < 6
    finally:
        if pid is not None:
            assert_backend_closed(pid)


@pytest.mark.integration
def test_read_only_probe_reuses_actual_singleton_evidence_and_runtime(monkeypatch):
    identity = "readiness-" + uuid4().hex
    monkeypatch.setattr(readiness, "RUNTIME_ID", identity)
    monkeypatch.setattr(readiness.settings, "schedule_enabled", True)
    with transaction() as session:
        original = load_schedule(session)
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="jma",
            catalogue_namespace=identity,
            west=140,
            east=140.5,
            south=35,
            north=35.5,
        )
    )
    try:
        requested = update_schedule(
            ScheduleUpdate(
                expected_revision=original["revision"],
                spec={"scope": "selected", "experiment_ids": [experiment.id], "kinds": ["evaluate"]},
                reason="Owned readiness acceptance scope",
            )
        )
        with worker_session() as owner:
            with transaction() as session:
                checked_at = session.scalar(text("SELECT clock_timestamp()"))
                session.add(
                    SchedulerRuntime(
                        id=identity,
                        revision=requested["revision"],
                        observed_at=checked_at,
                        owner_token=owner.token,
                        status="applied",
                        effective_enabled=True,
                        details={"spec_hash": schedule_spec_hash(requested["spec"])},
                    )
                )
            with readiness.readiness_session() as session:
                status = readiness.operational_status(
                    session, session.scalar(text("SELECT clock_timestamp()"))
                )
                assert status["worker"].status == "fresh" and status["worker"].owner_token == owner.token
                assert status["scheduler"]["status"] == "ok"
        with readiness.readiness_session() as session:
            status = readiness.operational_status(session, session.scalar(text("SELECT clock_timestamp()")))
            assert status["worker"].status == "offline" and status["scheduler"]["status"] == "failed"
    finally:
        with transaction() as session:
            revision = load_schedule(session)["revision"]
        update_schedule(
            ScheduleUpdate(
                expected_revision=revision,
                spec=original["spec"],
                reason="Restore policy after readiness acceptance",
            )
        )
