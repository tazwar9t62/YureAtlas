"""Bounded read-only diagnostics, independent of the application's busy pool."""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .config import settings
from .schedule_configuration import _scope, get_worker_status, load_schedule, schedule_spec_hash
from .tables import SchedulerRuntime
from .timeutil import utc

EXPECTED_SCHEMA_REVISION = "010"
CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 2000
LOCK_TIMEOUT_MS = 1000
TRANSACTION_TIMEOUT_MS = 5000
APPLICATION_FRESH_SECONDS = 90
APPLICATION_FUTURE_TOLERANCE_SECONDS = 5
RUNTIME_ID = "default"


@contextmanager
def readiness_session():
    """Use one disposable connection with PostgreSQL 17 transaction limits.

    Startup options also cover the driver's initial connection queries. No
    shared-pool checkout or pre-ping can delay this diagnostic. The server's
    transaction timeout bounds all subsequent checks together; connect_timeout
    separately bounds connection establishment. These are database limits, not
    a promise to interrupt arbitrary operating-system or network stalls.
    """
    engine = create_engine(
        settings.database_url,
        poolclass=NullPool,
        pool_pre_ping=False,
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "options": (
                f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={LOCK_TIMEOUT_MS} "
                f"-c transaction_timeout={TRANSACTION_TIMEOUT_MS}"
            ),
        },
    )
    try:
        with (
            engine.connect() as connection,
            Session(bind=connection, expire_on_commit=False) as session,
            session.begin(),
        ):
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session
    finally:
        engine.dispose()


def schema_status(session):
    """Require exactly the supported head; do not inspect current ORM tables yet."""
    exists = session.scalar(text("SELECT to_regclass('alembic_version')")) is not None
    observed = (
        list(session.scalars(text("SELECT version_num FROM alembic_version LIMIT 2"))) if exists else []
    )
    compatible = observed == [EXPECTED_SCHEMA_REVISION]
    return {
        "status": "ok" if compatible else "failed",
        "code": "schema_current" if compatible else "schema_mismatch" if exists else "schema_missing",
        "message": (
            "The database schema matches this installation"
            if compatible
            else "Apply this installation's migrations before using the database"
        ),
        "expected": EXPECTED_SCHEMA_REVISION,
        "observed": observed,
    }


def operational_status(session, checked_at):
    """Inspect worker ownership and a recent application of the requested policy."""
    checked_at = utc(checked_at)
    worker = get_worker_status(session, checked_at)
    requested = load_schedule(session)
    runtime = session.get(SchedulerRuntime, RUNTIME_ID)
    base = {
        "revision": requested["revision"],
        "applied_revision": runtime.revision if runtime else None,
        "observed_at": runtime.observed_at if runtime else None,
    }

    def result(status, code, message):
        return {"worker": worker, "scheduler": {**base, "status": status, "code": code, "message": message}}

    if worker.status != "fresh":
        return result(
            "failed", "scheduler_worker_unverified", "A current worker is required to verify scheduling"
        )
    if worker.environment_enabled is None:
        return result(
            "failed", "scheduler_environment_unknown", "The worker has not reported its schedule setting"
        )
    if worker.environment_enabled is False:
        return result(
            "ok",
            "scheduler_environment_disabled",
            "Automatic scheduling is intentionally disabled by the worker environment",
        )
    spec = requested["spec"]
    effective = bool(
        spec["enabled"] and spec["kinds"] and (spec["scope"] != "selected" or spec["experiment_ids"])
    )
    if effective:
        experiments, complete = _scope(session, spec)
        if not complete:
            return result(
                "failed", "scheduler_scope_exceeded", "The active schedule scope exceeds 100 real experiments"
            )
        effective = bool(experiments) and (
            any(kind != "ingest" for kind in spec["kinds"])
            or any(experiment.config["provider"] == "usgs" for experiment in experiments)
        )
    if runtime is None:
        return result(
            "failed", "scheduler_not_applied", "The worker has not yet applied the schedule configuration"
        )
    if runtime.owner_token != worker.owner_token:
        return result(
            "failed",
            "scheduler_owner_mismatch",
            "The current worker has not acknowledged the schedule configuration",
        )
    details = runtime.details if isinstance(runtime.details, dict) else {}
    if runtime.revision != requested["revision"] or details.get("spec_hash") != schedule_spec_hash(
        requested["spec"]
    ):
        return result(
            "failed",
            "scheduler_configuration_pending",
            "The worker has not yet applied the requested schedule revision",
        )
    if runtime.status not in {"applied", "disabled"} or runtime.effective_enabled != (
        runtime.status == "applied"
    ):
        return result(
            "failed", "scheduler_application_invalid", "The recorded schedule application is inconsistent"
        )
    if runtime.effective_enabled != effective:
        return result(
            "failed", "scheduler_scope_pending", "The worker has not yet applied the current experiment scope"
        )
    age = (checked_at - utc(runtime.observed_at)).total_seconds()
    if age < -APPLICATION_FUTURE_TOLERANCE_SECONDS:
        return result(
            "failed", "scheduler_clock_skew", "Schedule application time is ahead of the database clock"
        )
    if age > APPLICATION_FRESH_SECONDS:
        return result(
            "failed",
            "scheduler_application_stale",
            "The worker has not refreshed schedule application within 90 seconds",
        )
    return result(
        "ok",
        "scheduler_current" if runtime.effective_enabled else "scheduler_policy_disabled",
        "The current worker recently applied the requested schedule configuration",
    )
