"""Audited schedule requests and observable worker policy application."""

from datetime import timedelta

from sqlalchemy import and_, or_, select, text

from .artifacts import digest
from .config import settings
from .db import transaction
from .execution import WorkerOwner, holds_worker_lock
from .schedule_schemas import (
    NextScheduleRun,
    ScheduleApplication,
    ScheduleHistory,
    ScheduleRecovery,
    ScheduleRevision,
    ScheduleSpec,
    ScheduleStatus,
    ScheduleUpdate,
    ScheduleWorker,
)
from .tables import Experiment, Job, ScheduleConfig, SchedulerRuntime, WorkerState
from .timeutil import JST, next_day, utc

MAX_EXPERIMENTS = 100
RUNTIME_ID = "default"
WORKER_FRESH_SECONDS = 90
WORKER_FUTURE_TOLERANCE_SECONDS = 5


class ScheduleConflict(ValueError):
    pass


def lock_schedule(session):
    # Configuration changes, planning and claims take this lock before any
    # worker/job control locks. A completed disable cannot race a later claim.
    session.execute(text("SET LOCAL statement_timeout = '10s'"))
    session.execute(text("SELECT pg_advisory_xact_lock(8247714, 1)"))


def schedule_spec_hash(spec):
    return digest(ScheduleSpec.model_validate(spec).model_dump(mode="json"))


def _revision(row):
    if row is None:
        return {
            "id": None,
            "revision": 0,
            "created_at": None,
            "spec": ScheduleSpec().model_dump(mode="json"),
            "reason": "Installation defaults; no persisted schedule request yet",
        }
    return {
        "id": row.id,
        "revision": row.revision,
        "created_at": row.created_at,
        "spec": ScheduleSpec.model_validate(row.spec).model_dump(mode="json"),
        "reason": row.reason,
    }


def load_schedule(session):
    return _revision(session.scalar(select(ScheduleConfig).order_by(ScheduleConfig.revision.desc()).limit(1)))


def update_schedule(body: ScheduleUpdate):
    body = ScheduleUpdate.model_validate(body)
    spec = body.spec.model_dump(mode="json")
    with transaction() as session:
        lock_schedule(session)
        current = load_schedule(session)
        if body.expected_revision != current["revision"]:
            raise ScheduleConflict("Schedule settings changed; reload the current revision before saving")
        if spec["scope"] == "selected" and spec["experiment_ids"]:
            found = set(
                session.scalars(
                    select(Experiment.id).where(
                        Experiment.id.in_(spec["experiment_ids"]), Experiment.synthetic == False
                    )
                )
            )
            if found != set(spec["experiment_ids"]):
                raise ValueError("Selected schedule scope must contain only existing real experiments")
        row = ScheduleConfig(
            revision=current["revision"] + 1,
            created_at=session.scalar(text("SELECT clock_timestamp()")),
            spec=spec,
            reason=body.reason,
        )
        session.add(row)
        session.flush()
        return _revision(row)


def get_schedule_history(*, limit=25, before_revision=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Schedule history limit must be an integer between 1 and 100")
    if before_revision is not None and (type(before_revision) is not int or before_revision < 1):
        raise ValueError("Schedule history cursor must be a positive revision")
    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        query = select(ScheduleConfig)
        if before_revision is not None:
            query = query.where(ScheduleConfig.revision < before_revision)
        rows = list(session.scalars(query.order_by(ScheduleConfig.revision.desc()).limit(limit + 1)))
        more = len(rows) > limit
        visible = rows[:limit]
        return ScheduleHistory(
            items=[ScheduleRevision(**_revision(row)) for row in visible],
            has_more=more,
            next_before_revision=visible[-1].revision if more else None,
        )


def automatic_job_predicate(session):
    """Pause queued scheduled work without cancelling it or blocking manual work."""
    manual = ~Job.payload.contains({"scheduled": True})
    if not settings.schedule_enabled:
        return manual
    spec = load_schedule(session)["spec"]
    if not spec["enabled"] or not spec["kinds"]:
        return manual
    experiments, complete = _scope(session, spec)
    if not complete or not experiments:
        return manual
    experiment_id = Job.payload["experiment_id"].as_string()
    usgs_ids = [item.id for item in experiments if item.config["provider"] == "usgs"]
    return or_(
        manual,
        and_(
            Job.kind.in_(spec["kinds"]),
            experiment_id.in_([item.id for item in experiments]),
            or_(Job.kind != "ingest", experiment_id.in_(usgs_ids)),
        ),
    )


def get_worker_status(session, checked_at):
    """Read the registered process gate and actual singleton, without taking its locks."""
    row = session.get(WorkerState, "scheduler")
    if row is None:
        return ScheduleWorker(
            status="missing",
            heartbeat=None,
            owner_token=None,
            environment_enabled=None,
            reason="No worker has registered in this installation",
        )
    details = row.details if isinstance(row.details, dict) else {}
    gate = details.get("schedule_environment_enabled")
    base = {
        "heartbeat": row.heartbeat,
        "owner_token": details.get("owner_token") if isinstance(details.get("owner_token"), str) else None,
        "environment_enabled": gate if type(gate) is bool else None,
    }
    try:
        token, pid = details["owner_token"], details["pid"]
        if not isinstance(token, str) or not token or type(pid) is not int:
            raise ValueError("Invalid worker ownership")
        owner = WorkerOwner(token, pid, utc(details["backend_start"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return ScheduleWorker(**base, status="unregistered", reason="Worker ownership metadata is incomplete")
    if not holds_worker_lock(session, owner):
        return ScheduleWorker(
            **base, status="offline", reason="The registered worker no longer holds its singleton session"
        )
    age = (checked_at - row.heartbeat).total_seconds()
    if age < -WORKER_FUTURE_TOLERANCE_SECONDS:
        return ScheduleWorker(
            **base, status="clock_skew", reason="Worker heartbeat is ahead of the database clock"
        )
    if age > WORKER_FRESH_SECONDS:
        return ScheduleWorker(**base, status="stale", reason="Worker heartbeat is older than 90 seconds")
    return ScheduleWorker(**base, status="fresh", reason="Worker singleton and heartbeat are current")


def _scope(session, spec):
    query = select(Experiment).where(Experiment.synthetic == False)
    if spec["scope"] == "selected":
        query = query.where(Experiment.id.in_(spec["experiment_ids"]))
    rows = list(
        session.scalars(query.order_by(Experiment.created_at, Experiment.id).limit(MAX_EXPERIMENTS + 1))
    )
    return rows, len(rows) <= MAX_EXPERIMENTS


def get_schedule_status():
    from .scheduling import RECONCILE_SECONDS, next_regular_due

    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        requested = load_schedule(session)
        spec = requested["spec"]
        experiments, complete_scope = _scope(session, spec)
        kinds = (
            [
                kind
                for kind in spec["kinds"]
                if kind != "ingest" or any(item.config["provider"] == "usgs" for item in experiments)
            ]
            if experiments
            else []
        )
        worker = get_worker_status(session, checked_at)
        runtime = session.get(SchedulerRuntime, RUNTIME_ID)
        application = (
            ScheduleApplication(
                revision=runtime.revision,
                observed_at=runtime.observed_at,
                owner_token=runtime.owner_token,
                effective_enabled=runtime.effective_enabled,
                status=runtime.status,
                details=runtime.details,
            )
            if runtime
            else None
        )
        if not spec["enabled"]:
            effective, reason = False, "Persisted schedule settings are disabled"
        elif not complete_scope:
            effective, reason = (
                False,
                "More than 100 real experiments are selected; choose a bounded selected scope",
            )
        elif not kinds:
            effective, reason = False, "No automatic job kinds apply to the selected experiment scope"
        elif worker.status != "fresh" or worker.environment_enabled is None:
            effective, reason = (
                None,
                "Effective scheduling is unverified until a current worker reports its process setting",
            )
        elif not worker.environment_enabled:
            effective, reason = (
                False,
                "The worker process has SCHEDULE_ENABLED=false; persisted requests cannot override it",
            )
        else:
            effective, reason = (
                True,
                "The current worker and requested configuration permit automatic scheduling",
            )
        if worker.status != "fresh" or worker.environment_enabled is None:
            application_status = "worker_unverified"
        elif worker.environment_enabled is False:
            application_status = "environment_disabled"
        elif (
            application is not None
            and application.owner_token == worker.owner_token
            and application.revision == requested["revision"]
            and application.details.get("spec_hash") == schedule_spec_hash(spec)
            and application.effective_enabled == effective
        ):
            application_status = "current"
        else:
            application_status = "pending"
        runs = []
        eligible = None
        next_check = None
        if worker.status == "fresh" and worker.environment_enabled:
            observed = (
                application.observed_at
                if application and application.owner_token == worker.owner_token
                else checked_at
            )
            next_check = min(
                checked_at + timedelta(seconds=RECONCILE_SECONDS),
                max(checked_at, observed + timedelta(seconds=RECONCILE_SECONDS)),
            )
            if effective:
                for kind, due in next_regular_due(checked_at).items():
                    if kind in kinds:
                        runs.append(
                            NextScheduleRun(
                                kind=kind,
                                at_utc=due,
                                at_jst=due.astimezone(JST).isoformat(),
                                forecast_target_start=due + timedelta(minutes=20)
                                if kind == "forecast"
                                else None,
                            )
                        )
                upcoming, _ = next_day(checked_at)
                if "forecast" in kinds and checked_at >= upcoming - timedelta(minutes=20):
                    eligible = upcoming
        return ScheduleStatus(
            checked_at=checked_at,
            requested=ScheduleRevision(**requested),
            api_environment_enabled=settings.schedule_enabled,
            worker=worker,
            effective_enabled=effective,
            effective_reason=reason,
            application_status=application_status,
            last_application=application,
            scope_count=len(experiments),
            scope_complete=complete_scope,
            applicable_kinds=kinds,
            next_regular_runs=runs,
            recovery=ScheduleRecovery(
                next_check_estimate=next_check,
                eligible_forecast_target=eligible,
                policy="Regular times are cadence only. Each 30-second check separately coalesces overdue safe work and audits at most 31 expired forecast targets; existing keys win. An eligible current target may already have a decision. Running jobs require explicit cancellation.",
            ),
        )
