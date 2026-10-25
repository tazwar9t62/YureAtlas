import math
from datetime import timedelta
from numbers import Real

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from .db import Session, transaction
from .execution import (
    Cancelled,  # noqa: F401 - preserve the public exception used by providers
    JobOwnershipLost,
    verify_attempt,
    verify_worker,
)
from .tables import Job, WorkerState, now, uid

LEASE_DURATION = timedelta(minutes=2)
MAX_ATTEMPTS = 3
CLAIM_BATCH = 100


def enqueue(kind: str, payload: dict, key: str, *, session=None) -> Job:
    return _enqueue(kind, payload, key, session=session)


def enqueue_missed(kind: str, payload: dict, key: str, reason: str, *, session=None) -> Job:
    """Insert an absent missed decision without modifying existing work.

    A missed decision must never be briefly claimable as queued. The unique-key
    insert also resolves concurrent startup/cron decisions without a later
    status update that could overwrite the winner.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A missed job requires a reason")
    return _enqueue(kind, payload, key, missed_reason=reason, session=session)


def _job_values(kind: str, payload: dict, key: str, *, missed_reason: str | None = None):
    timestamp = now()
    execution = None
    result = {}
    if kind == "forecast":
        from .forecast_execution import pin_forecast_target

        execution = pin_forecast_target(payload, timestamp)
        if missed_reason is not None:
            from .forecast_errors import PublicationDeadlineMissed

            result = PublicationDeadlineMissed(
                execution["start"], "schedule_reconciliation", timestamp
            ).result
    return {
        "id": uid(),
        "created_at": timestamp,
        "kind": kind,
        "payload": payload,
        "logical_key": key,
        "status": "missed" if missed_reason is not None else "queued",
        "attempts": 0,
        "progress": 0,
        "cancel_requested": False,
        "result": result,
        "execution": execution,
        "error": missed_reason,
        "heartbeat": timestamp if missed_reason is not None else None,
        "lease_until": None,
    }


def _enqueue(kind: str, payload: dict, key: str, *, missed_reason: str | None = None, session=None) -> Job:
    if session is None:
        with transaction() as owned:
            return _enqueue(kind, payload, key, missed_reason=missed_reason, session=owned)
    session.execute(
        insert(Job)
        .values(**_job_values(kind, payload, key, missed_reason=missed_reason))
        .on_conflict_do_nothing(index_elements=[Job.logical_key])
    )
    return session.scalar(select(Job).where(Job.logical_key == key))


def enqueue_batch(plans: list[dict], *, session) -> list[str]:
    """Insert bounded chunks within the caller's decision/cursor transaction."""
    inserted = []
    # Fourteen columns per row: comfortably below PostgreSQL's bind limit.
    for offset in range(0, len(plans), 200):
        values = [_job_values(**plan) for plan in plans[offset : offset + 200]]
        inserted.extend(
            session.scalars(
                insert(Job)
                .values(values)
                .on_conflict_do_nothing(index_elements=[Job.logical_key])
                .returning(Job.logical_key)
            )
        )
    return inserted


def claim(owner) -> Job | None:
    # Control transactions deliberately use their explicit owner checks rather
    # than recursively applying a dispatch context's final commit guard.
    with Session.begin() as s:
        worker = verify_worker(s, owner)
        candidates = s.scalars(
            select(Job)
            .where(
                or_(
                    Job.status == "queued",
                    (Job.status == "running")
                    & or_(Job.lease_until.is_(None), Job.lease_until <= func.clock_timestamp()),
                )
            )
            .order_by(Job.created_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(CLAIM_BATCH)
        )
        timestamp = s.scalar(select(func.clock_timestamp()))
        worker.heartbeat = timestamp
        worker.details = {**worker.details, "active_job": None}
        for job in candidates:
            if job.cancel_requested or job.attempts >= MAX_ATTEMPTS:
                job.status = "cancelled" if job.cancel_requested else "failed"
                job.error = "Cancelled" if job.cancel_requested else "Lease recovery retry limit reached"
                job.heartbeat = timestamp
                job.lease_until = None
                continue
            job.status = "running"
            job.attempts += 1
            job.attempt_token = uid()
            job.worker_token = owner.token
            job.heartbeat = timestamp
            job.lease_until = timestamp + LEASE_DURATION
            job.error = None
            job.result = {}
            job.progress = 0
            worker.details = {**worker.details, "active_job": job.id}
            return job
        return None


def checkpoint(job_id: str, attempt_token: str, owner, progress: float | None = None):
    if progress is not None and (
        isinstance(progress, bool) or not isinstance(progress, Real) or not math.isfinite(progress)
    ):
        raise ValueError("Progress must be a finite number")
    with Session.begin() as s:
        job = verify_attempt(s, job_id, attempt_token, owner)
        timestamp = s.scalar(select(func.clock_timestamp()))
        job.heartbeat = timestamp
        job.lease_until = timestamp + LEASE_DURATION
        if progress is not None:
            job.progress = float(min(1, max(0, progress)))
        # verify_attempt already locked this worker row before the job row.
        worker = s.get(WorkerState, "scheduler")
        worker.heartbeat = timestamp
        worker.details = {**worker.details, "active_job": job.id}
        return job


def finish(
    job_id: str,
    attempt_token: str,
    owner,
    status: str,
    result: dict | None = None,
    error: str | None = None,
):
    if status not in {"completed", "failed", "cancelled", "missed"}:
        raise ValueError("A finished job requires a terminal status")
    with Session.begin() as s:
        job = verify_attempt(s, job_id, attempt_token, owner, allow_cancelled=True, require_lease=False)
        timestamp = s.scalar(select(func.clock_timestamp()))
        if job.cancel_requested or status == "cancelled":
            status, result = "cancelled", None
            error = "Cancellation requested; earlier committed work is retained"
            job.cancel_requested = True
        elif job.lease_until is None or job.lease_until <= timestamp:
            raise JobOwnershipLost("Job lease expired before completion; this attempt cannot finish it")
        job.status = status
        job.result = result or {}
        job.error = error
        job.heartbeat = timestamp
        job.lease_until = None
        if status == "completed":
            job.progress = 1
        worker = s.get(WorkerState, "scheduler")
        worker.heartbeat = timestamp
        worker.details = {**worker.details, "active_job": None}
        return job


def request_cancel(job_id: str) -> Job | None:
    with Session.begin() as s:
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status not in {"queued", "running"}:
            return job
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.error = "Cancelled before execution"
            job.heartbeat = s.scalar(select(func.clock_timestamp()))
            job.lease_until = None
        return job


def worker_heartbeat(owner, active_job: str | None = None):
    with Session.begin() as s:
        worker = verify_worker(s, owner)
        worker.heartbeat = s.scalar(select(func.clock_timestamp()))
        worker.details = dict(worker.details, active_job=active_job)
        return worker
