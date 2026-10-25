"""Session-scoped USGS attempts, fenced by the short catalogue write lock."""

import hashlib
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from .db import engine

# Two-int advisory keys are distinct from the existing one-bigint source write
# locks, experiment lock, and worker lock. Hash collisions only serialize work.
USGS_ATTEMPT_NAMESPACE = 8247713


class SourceAttemptBusy(ValueError):
    """Another attempt owns this source; retry after it finishes."""


class SourceAttemptLost(ValueError):
    """This attempt's original database session no longer owns the source."""


def attempt_key(source_id):
    return int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:4], "big", signed=True)


@dataclass(frozen=True)
class AttemptOwner:
    source_id: str
    pid: int
    backend_start: datetime


def verify_usgs_writer(session, source_id, owner=None):
    """Called only while holding this source's transaction advisory lock.

    If the owner disconnects immediately after this check, a successor's started
    receipt still waits for this write to finish. Every later old-owner write is
    fenced, so it cannot append supplements after the successor completes.
    """
    holders = session.execute(
        text("""
            SELECT locks.pid, activity.backend_start
            FROM pg_locks AS locks
            JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
            WHERE locks.locktype = 'advisory' AND locks.granted
              AND locks.database = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND locks.classid::bigint = :namespace AND locks.objid::bigint = :key
              AND locks.objsubid = 2
        """),
        {"namespace": USGS_ATTEMPT_NAMESPACE, "key": attempt_key(source_id) % (1 << 32)},
    ).all()
    if owner is None:
        if holders:
            raise SourceAttemptBusy("USGS source is being reconciled; retry this write after it finishes")
    elif owner.source_id != source_id or (owner.pid, owner.backend_start) not in holders:
        raise SourceAttemptLost("USGS attempt lost its source lock; retry the complete attempt")


def release_attempt(connection, source_id):
    released = connection.scalar(
        text("SELECT pg_advisory_unlock(:namespace, :key)"),
        {"namespace": USGS_ATTEMPT_NAMESPACE, "key": attempt_key(source_id)},
    )
    if not released:
        raise SourceAttemptLost("USGS attempt no longer held its source lock during cleanup")


@contextmanager
def usgs_attempt(source_id):
    """Try once; keep an idle AUTOCOMMIT session, never a network-long transaction."""
    connection = None
    acquired = False
    try:
        connection = engine.connect()
        connection.execution_options(isolation_level="AUTOCOMMIT")
        # The pool checkout is bounded by its configured timeout; the advisory
        # try-lock never waits, and even its server-side statement is bounded.
        connection.execute(text("SET statement_timeout = '5s'"))
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :key)"),
            {"namespace": USGS_ATTEMPT_NAMESPACE, "key": attempt_key(source_id)},
        )
        if not acquired:
            raise SourceAttemptBusy("Another USGS attempt owns this source; retry after it finishes")
        pid, backend_start = connection.execute(
            text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()")
        ).one()
        yield AttemptOwner(source_id, pid, backend_start)
    finally:
        primary = sys.exception()
        cleanup_errors = []
        if connection is not None:
            if acquired:
                try:
                    release_attempt(connection, source_id)
                except Exception as error:  # noqa: BLE001 - finish cleanup and preserve primary errors
                    cleanup_errors.append(error)
            # Physically close even after failed unlock. A pooled session may
            # never return carrying an advisory lock or this session's timeout.
            try:
                connection.invalidate()
            except Exception as error:  # noqa: BLE001 - finish cleanup and preserve primary errors
                cleanup_errors.append(error)
            try:
                connection.close()
            except Exception as error:  # noqa: BLE001 - finish cleanup and preserve primary errors
                cleanup_errors.append(error)
        if cleanup_errors:
            if primary is None:
                raise cleanup_errors[0]
            for error in cleanup_errors:
                primary.add_note(f"USGS attempt cleanup failed: {type(error).__name__}")
