from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import transaction
from .tables import Job, now, uid


class Cancelled(Exception):
    pass


def enqueue(kind: str, payload: dict, key: str) -> Job:
    with transaction() as s:
        s.execute(insert(Job).values(id=uid(), created_at=now(), kind=kind, payload=payload, logical_key=key,
                                    status="queued", attempts=0, progress=0, cancel_requested=False, result={})
                  .on_conflict_do_nothing(index_elements=[Job.logical_key]))
        return s.scalar(select(Job).where(Job.logical_key == key))


def claim() -> Job | None:
    with transaction() as s:
        # Retries preserve the logical job key and every publication has its own
        # unique key. Expired leases may be safely reclaimed after process death.
        job = s.scalar(select(Job).where(
            or_(Job.status == "queued", (Job.status == "running") & (Job.lease_until < now())))
            .order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        if job.cancel_requested or job.attempts >= 3:
            job.status = "cancelled" if job.cancel_requested else "failed"
            job.error = "Cancelled" if job.cancel_requested else "Lease recovery retry limit reached"
            return None
        job.status = "running"
        job.attempts += 1
        job.heartbeat = now()
        job.lease_until = now() + timedelta(minutes=2)
        return job


def checkpoint(job_id: str, progress: float | None = None):
    with transaction() as s:
        job = s.get(Job, job_id)
        if job.cancel_requested:
            raise Cancelled("Cancellation requested")
        job.heartbeat = now()
        job.lease_until = now() + timedelta(minutes=2)
        if progress is not None:
            job.progress = min(1, max(0, progress))


def finish(job_id: str, status: str, result: dict | None = None, error: str | None = None):
    with transaction() as s:
        s.execute(update(Job).where(Job.id == job_id).values(
            status=status, result=result or {}, error=error, heartbeat=now(), lease_until=None,
            progress=1 if status == "completed" else 0))
