"""An idle AUTOCOMMIT singleton session and short, serialized worker epochs."""

import sys
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .config import settings
from .db import Session, engine
from .execution import WORKER_LOCK_KEY, WORKER_STATE_ID, WorkerOwner, WorkerOwnershipLost, holds_worker_lock
from .tables import WorkerState


def register_worker(owner: WorkerOwner):
    """Acknowledge takeover only after earlier fenced domain commits finish."""
    # Control transactions deliberately use Session, never the scoped domain
    # transaction helper. They acquire no domain locks while holding this row.
    with Session.begin() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(
            insert(WorkerState)
            .values(id=WORKER_STATE_ID, heartbeat=text("clock_timestamp()"), details={})
            .on_conflict_do_nothing(index_elements=[WorkerState.id])
        )
        state = session.scalar(
            select(WorkerState)
            .where(WorkerState.id == WORKER_STATE_ID)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not holds_worker_lock(session, owner):
            raise WorkerOwnershipLost("Worker lost its singleton session before epoch registration")
        state.heartbeat = session.scalar(text("SELECT clock_timestamp()"))
        state.details = {
            **(state.details or {}),
            "owner_token": owner.token,
            "pid": owner.pid,
            "backend_start": owner.backend_start.isoformat(),
            "active_job": None,
            "schedule_environment_enabled": settings.schedule_enabled,
        }


@contextmanager
def worker_session():
    """Own one worker without holding a transaction during CPU/network work."""
    connection = None
    acquired = False
    try:
        connection = engine.connect()
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.execute(text("SET statement_timeout = '5s'"))
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK_KEY})
        if not acquired:
            raise WorkerOwnershipLost("A YureAtlas worker is already running")
        pid, backend_start = connection.execute(
            text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()")
        ).one()
        owner = WorkerOwner(uuid4().hex, pid, backend_start)
        register_worker(owner)
        yield owner
    finally:
        primary = sys.exception()
        cleanup_errors = []
        if connection is not None:
            if acquired:
                try:
                    released = connection.scalar(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK_KEY}
                    )
                    if not released:
                        raise WorkerOwnershipLost("Worker no longer held its singleton lock during cleanup")
                except Exception as error:  # noqa: BLE001 - complete cleanup without masking primary failure
                    cleanup_errors.append(error)
            # Discard the physical session even when unlock fails; no advisory
            # ownership or session timeout may escape back into the pool.
            try:
                connection.invalidate()
            except Exception as error:  # noqa: BLE001 - preserve primary failure and continue cleanup
                cleanup_errors.append(error)
            try:
                connection.close()
            except Exception as error:  # noqa: BLE001 - preserve primary failure after other cleanup
                cleanup_errors.append(error)
        if cleanup_errors:
            if primary is None:
                raise cleanup_errors[0]
            for error in cleanup_errors:
                primary.add_note(f"Worker session cleanup failed: {type(error).__name__}")
