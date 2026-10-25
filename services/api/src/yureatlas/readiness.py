"""Bounded dependency checks with sanitized, independently actionable results."""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .readiness_artifacts import probe_artifact_root
from .readiness_database import EXPECTED_SCHEMA_REVISION, operational_status, readiness_session, schema_status
from .readiness_prerequisites import ReadinessPrerequisites, inspect_prerequisites
from .readiness_schemas import (
    ReadinessCheck,
    ReadinessChecks,
    ReadinessResponse,
    SchedulerCheck,
    SchemaCheck,
    WorkerCheck,
)


def inspect_readiness() -> ReadinessResponse:
    checked_at = datetime.now(UTC)
    database = ReadinessCheck(
        status="failed", code="database_unavailable", message="Database connectivity has not been verified"
    )
    schema = SchemaCheck(
        status="skipped",
        code="schema_unverified",
        message="A database connection is required to verify schema compatibility",
        expected=EXPECTED_SCHEMA_REVISION,
    )
    worker = WorkerCheck(
        status="skipped",
        code="worker_unverified",
        message="A compatible database is required to inspect the worker",
    )
    scheduler = SchedulerCheck(
        status="skipped",
        code="scheduler_unverified",
        message="A compatible database is required to inspect scheduling",
    )
    prerequisites = ReadinessPrerequisites(
        status="unavailable", message="A compatible database is required to inspect research prerequisites"
    )
    try:
        with readiness_session() as session:
            checked_at = session.scalar(text("SELECT clock_timestamp()"))
            database = ReadinessCheck(
                status="ok",
                code="database_available",
                message="The database accepted a bounded read-only check",
            )
            schema = SchemaCheck(**schema_status(session))
            if schema.status == "ok":
                try:
                    operational = operational_status(session, checked_at)
                    state = operational["worker"]
                    worker = WorkerCheck(
                        status="ok" if state.status == "fresh" else "failed",
                        code=f"worker_{state.status}",
                        message=state.reason,
                        worker=state,
                    )
                    scheduler = SchedulerCheck(**operational["scheduler"])
                except (ValueError, TypeError, KeyError):
                    scheduler = SchedulerCheck(
                        status="failed",
                        code="operational_state_invalid",
                        message="Saved worker or schedule metadata is invalid; inspect configuration and restart the worker",
                    )
                try:
                    prerequisites = inspect_prerequisites(session, checked_at)
                except (ValueError, TypeError, KeyError):
                    prerequisites = ReadinessPrerequisites(
                        status="unavailable",
                        message="Research prerequisite metadata could not be interpreted; inspect source and model records",
                    )
    except (SQLAlchemyError, OSError):
        database = ReadinessCheck(
            status="failed",
            code="database_probe_failed",
            message="Database access failed or exceeded its time limit; verify the database service and connection settings",
        )
    artifacts = ReadinessCheck(**probe_artifact_root(settings.artifact_dir))
    checks = ReadinessChecks(
        database=database, schema=schema, artifacts=artifacts, worker=worker, scheduler=scheduler
    )
    ready = all(getattr(checks, name).status == "ok" for name in ReadinessChecks.model_fields)
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        checked_at=checked_at,
        checks=checks,
        prerequisites=prerequisites,
    )
