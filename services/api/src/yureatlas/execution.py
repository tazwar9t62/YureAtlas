"""Worker execution identity and the final, transaction-held publication fence."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text

from .tables import Job, WorkerState
from .timeutil import utc

WORKER_STATE_ID = "scheduler"
WORKER_LOCK_KEY = 8247711


class Cancelled(Exception):
    """The current job's cancellation request reached a safe boundary."""


class LostOwnership(RuntimeError):
    """Execution may no longer commit changes on behalf of its former owner."""


class WorkerOwnershipLost(LostOwnership):
    """The worker no longer owns both its session lock and registered epoch."""


class JobOwnershipLost(LostOwnership):
    """The job's attempt, worker, status, or lease is no longer current."""


@dataclass(frozen=True)
class WorkerOwner:
    token: str
    pid: int
    backend_start: datetime


@dataclass(frozen=True)
class ExecutionContext:
    owner: WorkerOwner
    job_id: str | None = None
    attempt_token: str | None = None


_execution: ContextVar[ExecutionContext | None] = ContextVar("yureatlas_execution", default=None)


def current_execution() -> ExecutionContext | None:
    return _execution.get()


@contextmanager
def execution_scope(owner: WorkerOwner, *, job_id=None, attempt_token=None):
    """Bind the owner to this thread/task; child threads require explicit scopes."""
    if (job_id is None) != (attempt_token is None):
        raise ValueError("A job execution requires both job_id and attempt_token")
    token = _execution.set(ExecutionContext(owner, job_id, attempt_token))
    try:
        yield _execution.get()
    finally:
        _execution.reset(token)


def holds_worker_lock(session, owner: WorkerOwner) -> bool:
    """Check the real exclusive bigint advisory lock, including backend lifetime."""
    return bool(
        session.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks AS locks
                    JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
                    WHERE locks.locktype = 'advisory' AND locks.granted
                      AND locks.mode = 'ExclusiveLock'
                      AND locks.database = (SELECT oid FROM pg_database WHERE datname = current_database())
                      AND locks.classid::bigint = 0 AND locks.objid::bigint = :key
                      AND locks.objsubid = 1
                      AND locks.pid = :pid AND activity.backend_start = :backend_start
                )
            """),
            {"key": WORKER_LOCK_KEY, "pid": owner.pid, "backend_start": owner.backend_start},
        )
    )


def verify_worker(session, owner: WorkerOwner) -> WorkerState:
    """Hold the epoch row until commit, serializing old writes with takeover.

    Session-lock loss after this check cannot let a successor acknowledge its
    epoch before this transaction finishes: registration takes the same row.
    Every subsequent old-owner transaction must verify the new epoch again.
    """
    state = session.scalar(
        select(WorkerState)
        .where(WorkerState.id == WORKER_STATE_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    details = state.details if state is not None and isinstance(state.details, dict) else {}
    try:
        registered_start = utc(details["backend_start"])
    except (KeyError, TypeError, ValueError, AttributeError):
        registered_start = None
    if (
        state is None
        or details.get("owner_token") != owner.token
        or details.get("pid") != owner.pid
        or registered_start != owner.backend_start
        or not holds_worker_lock(session, owner)
    ):
        raise WorkerOwnershipLost("Worker lost its singleton session or registered ownership epoch")
    return state


def verify_attempt(
    session,
    job_id,
    attempt_token,
    owner: WorkerOwner,
    *,
    allow_cancelled=False,
    require_lease=True,
) -> Job:
    """Lock worker then job, and check current ownership before any final write.

    allow_cancelled permits an acknowledged cancellation flag on a still-running
    owned attempt; it never permits changing an already terminal job.
    """
    verify_worker(session, owner)
    job = session.scalar(
        select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True)
    )
    if (
        job is None
        or job.status != "running"
        or job.worker_token != owner.token
        or job.attempt_token != attempt_token
        or not attempt_token
    ):
        raise JobOwnershipLost("Job attempt no longer belongs to this worker")
    if job.cancel_requested and not allow_cancelled:
        raise Cancelled("Job cancellation requested")
    if require_lease:
        # CURRENT_TIMESTAMP is frozen at transaction start, including lock wait.
        timestamp = session.scalar(text("SELECT clock_timestamp()"))
        if job.lease_until is None or job.lease_until <= timestamp:
            raise JobOwnershipLost("Job attempt lease expired; this attempt cannot commit")
    return job


def verify_execution(session):
    """Mandatory final guard for application transactions inside worker scopes."""
    execution = current_execution()
    if execution is None:
        return
    if execution.job_id is None:
        verify_worker(session, execution.owner)
    else:
        verify_attempt(session, execution.job_id, execution.attempt_token, execution.owner)
