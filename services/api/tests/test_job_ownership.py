"""Owned worker/job controls; every database case requires the disposable harness."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session as OrmSession
from yureatlas import jobs
from yureatlas.artifacts import canonical
from yureatlas.db import transaction
from yureatlas.execution import JobOwnershipLost, WorkerOwnershipLost
from yureatlas.tables import Job, WorkerState
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session


@pytest.fixture
def owned_jobs():
    ids = set()
    prefix = "ownership-" + uuid4().hex

    def restrict_claim_and_attempt_locks(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(item.get("entity") is Job for item in statement.column_descriptions)
        ):
            state.statement = statement.where(Job.id.in_(ids))

    def add(*rows):
        with transaction() as session:
            for row in rows:
                session.add(row)
            session.flush()
            ids.update(row.id for row in rows)
        return rows

    def new(**values):
        row = Job(kind="ping", logical_key=prefix + ":" + uuid4().hex, payload={}, **values)
        add(row)
        return row

    # Restrict only Job locking SELECTs. The production status predicate,
    # ordering, SKIP LOCKED and batch limit are retained, as are worker checks.
    event.listen(OrmSession, "do_orm_execute", restrict_claim_and_attempt_locks)
    try:
        yield SimpleNamespace(ids=ids, prefix=prefix, new=new, add=add)
    finally:
        event.remove(OrmSession, "do_orm_execute", restrict_claim_and_attempt_locks)
        # A failed assertion must not leave claimable work for later tests.
        # This updates only IDs created here; completed evidence is preserved.
        with transaction() as session:
            session.execute(
                update(Job)
                .where(Job.id.in_(ids), Job.status.in_(["queued", "running"]))
                .values(status="cancelled", cancel_requested=True, lease_until=None)
            )


def stored(identity):
    with transaction() as session:
        return canonical(
            dict(session.execute(select(Job.__table__).where(Job.id == identity)).mappings().one())
        )


def read(identity):
    with transaction() as session:
        return session.get(Job, identity)


def database_clock():
    with transaction() as session:
        return session.scalar(text("SELECT clock_timestamp()"))


def expire(identity):
    with transaction() as session:
        session.execute(
            update(Job)
            .where(Job.id == identity)
            .values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )


@pytest.mark.parametrize(
    "progress", [True, False, "0.5", complex(1, 0), float("nan"), float("inf"), -float("inf")]
)
def test_invalid_progress_is_rejected_before_database_work(monkeypatch, progress):
    def forbidden():
        pytest.fail("Invalid progress reached a database transaction")

    monkeypatch.setattr(jobs, "Session", SimpleNamespace(begin=forbidden))
    with pytest.raises(ValueError, match="finite number"):
        jobs.checkpoint("job", "attempt", object(), progress)


@pytest.mark.integration
def test_current_owner_uses_database_clock_clamps_progress_and_completes_ping(owned_jobs, monkeypatch):
    row = owned_jobs.new()
    monkeypatch.setattr(jobs, "now", lambda: utc("2099-01-01T00:00:00Z"))
    with worker_session() as owner:
        before = database_clock()
        claimed = jobs.claim(owner)
        after = database_clock()
        assert claimed.id == row.id and claimed.status == "running" and claimed.attempts == 1
        assert claimed.attempt_token and claimed.worker_token == owner.token
        assert before <= claimed.heartbeat <= after
        assert claimed.lease_until - claimed.heartbeat == timedelta(minutes=2)
        for progress, expected in ((-0.5, 0), (2, 1), (0.45, 0.45), (None, 0.45)):
            before = database_clock()
            current = jobs.checkpoint(row.id, claimed.attempt_token, owner, progress)
            after = database_clock()
            assert current.progress == expected
            assert before <= current.heartbeat <= after
            assert current.lease_until - current.heartbeat == timedelta(minutes=2)
        completed = jobs.finish(row.id, claimed.attempt_token, owner, "completed", result={"pong": True})
        assert completed.status == "completed" and completed.progress == 1
        assert completed.result == {"pong": True} and completed.lease_until is None
        with transaction() as session:
            assert session.get(WorkerState, "scheduler").details["active_job"] is None
        original = stored(row.id)
        with pytest.raises(JobOwnershipLost):
            jobs.checkpoint(row.id, claimed.attempt_token, owner, 0)
        assert stored(row.id) == original


@pytest.mark.integration
def test_concurrent_claims_grant_one_attempt_for_one_job(owned_jobs):
    row = owned_jobs.new()
    barrier = Barrier(3)
    with worker_session() as owner:

        def claim():
            barrier.wait(timeout=10)
            return jobs.claim(owner)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(claim) for _ in range(3)]
            results = [future.result(timeout=15) for future in futures]
        claimed = [result for result in results if result is not None]
        assert len(claimed) == 1 and claimed[0].id == row.id
        assert read(row.id).attempts == 1
        jobs.finish(row.id, claimed[0].attempt_token, owner, "completed")


@pytest.mark.integration
def test_expiry_and_reclaim_reject_old_attempt_without_changing_new_work(owned_jobs):
    row = owned_jobs.new()
    with worker_session() as owner:
        old = jobs.claim(owner)
        jobs.checkpoint(row.id, old.attempt_token, owner, 0.6)
        expire(row.id)
        expired = stored(row.id)
        for mutate in (
            lambda: jobs.checkpoint(row.id, old.attempt_token, owner, 0.9),
            lambda: jobs.finish(row.id, old.attempt_token, owner, "completed", result={"stale": True}),
        ):
            with pytest.raises(JobOwnershipLost, match="[Ll]ease"):
                mutate()
            assert stored(row.id) == expired
        current = jobs.claim(owner)
        assert current.id == old.id and current.attempts == 2
        assert current.attempt_token != old.attempt_token and current.worker_token == owner.token
        assert current.progress == 0 and current.error is None and current.result == {}
        jobs.checkpoint(row.id, current.attempt_token, owner, 0.25)
        active = stored(row.id)
        for mutate in (
            lambda: jobs.checkpoint(row.id, old.attempt_token, owner, 0.9),
            lambda: jobs.finish(row.id, old.attempt_token, owner, "failed", error="stale failure"),
        ):
            with pytest.raises(JobOwnershipLost):
                mutate()
            assert stored(row.id) == active
        jobs.finish(row.id, current.attempt_token, owner, "completed", result={"attempt": 2})


@pytest.mark.integration
def test_new_worker_cannot_use_previous_workers_unexpired_attempt(owned_jobs):
    row = owned_jobs.new()
    with worker_session() as first_owner:
        old = jobs.claim(first_owner)
    original = stored(row.id)
    with worker_session() as successor:
        assert successor.token != first_owner.token
        with pytest.raises(JobOwnershipLost):
            jobs.checkpoint(row.id, old.attempt_token, successor, 1)
        assert jobs.claim(successor) is None
        assert stored(row.id) == original
        expire(row.id)
        reclaimed = jobs.claim(successor)
        assert reclaimed.worker_token == successor.token and reclaimed.attempt_token != old.attempt_token
        jobs.finish(row.id, reclaimed.attempt_token, successor, "completed")


@pytest.mark.integration
def test_queued_cancellation_is_terminal_and_never_claimed(owned_jobs):
    row = owned_jobs.new()
    cancelled = jobs.request_cancel(row.id)
    assert cancelled.status == "cancelled" and cancelled.cancel_requested
    assert cancelled.attempts == 0 and cancelled.lease_until is None
    original = stored(row.id)
    assert jobs.request_cancel(row.id).id == row.id
    assert jobs.request_cancel(uuid4().hex) is None
    with worker_session() as owner:
        assert jobs.claim(owner) is None
    assert stored(row.id) == original


@pytest.mark.integration
@pytest.mark.parametrize("expired", [False, True])
def test_running_cancellation_fences_checkpoint_and_wins_completion_preserving_progress(owned_jobs, expired):
    row = owned_jobs.new()
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        jobs.checkpoint(row.id, attempt.attempt_token, owner, 0.4)
        requested = jobs.request_cancel(row.id)
        assert requested.status == "running" and requested.cancel_requested
        if expired:
            expire(row.id)
        flagged = stored(row.id)
        with pytest.raises(jobs.Cancelled):
            jobs.checkpoint(row.id, attempt.attempt_token, owner, 0.9)
        assert stored(row.id) == flagged
        cancelled = jobs.finish(row.id, attempt.attempt_token, owner, "completed", result={"discard": True})
        assert cancelled.status == "cancelled" and cancelled.result == {}
        assert cancelled.progress == 0.4 and cancelled.lease_until is None
        assert "earlier committed work" in cancelled.error
        final = stored(row.id)
        with pytest.raises(JobOwnershipLost):
            jobs.finish(row.id, attempt.attempt_token, owner, "failed", error="too late")
        assert stored(row.id) == final


@pytest.mark.integration
def test_completed_job_wins_later_cancellation_without_any_row_changes(owned_jobs):
    row = owned_jobs.new()
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        jobs.finish(row.id, attempt.attempt_token, owner, "completed", result={"retained": True})
        original = stored(row.id)
        result = jobs.request_cancel(row.id)
        assert result.status == "completed" and not result.cancel_requested
        assert stored(row.id) == original


@pytest.mark.integration
def test_concurrent_cancel_and_finish_have_one_consistent_terminal_outcome(owned_jobs):
    row = owned_jobs.new()
    barrier = Barrier(2)
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        jobs.checkpoint(row.id, attempt.attempt_token, owner, 0.3)

        def cancel():
            barrier.wait(timeout=10)
            return jobs.request_cancel(row.id)

        def finish():
            barrier.wait(timeout=10)
            return jobs.finish(row.id, attempt.attempt_token, owner, "completed", result={"saved": True})

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled, finished = pool.submit(cancel), pool.submit(finish)
            cancellation, completion = cancelled.result(timeout=15), finished.result(timeout=15)
        final = read(row.id)
        assert completion.status == final.status
        if final.status == "completed":
            assert cancellation.status == "completed"
            assert final.result == {"saved": True} and final.progress == 1 and not final.cancel_requested
        else:
            assert final.status == "cancelled" and cancellation.status == "running"
            assert final.result == {} and final.progress == 0.3 and final.cancel_requested


@pytest.mark.integration
def test_claim_skips_cancelled_and_exhausted_work_in_the_same_poll(owned_jobs):
    base = database_clock() - timedelta(minutes=5)
    cancelled = owned_jobs.new(created_at=base, cancel_requested=True, progress=0.2)
    exhausted = owned_jobs.new(
        created_at=base + timedelta(seconds=1), status="running", attempts=3, lease_until=base, progress=0.7
    )
    valid = owned_jobs.new(created_at=base + timedelta(seconds=2))
    with worker_session() as owner:
        attempt = jobs.claim(owner)
        assert attempt.id == valid.id
        assert read(cancelled.id).status == "cancelled" and read(cancelled.id).progress == 0.2
        assert read(exhausted.id).status == "failed" and read(exhausted.id).progress == 0.7
        assert "retry limit" in read(exhausted.id).error
        jobs.finish(attempt.id, attempt.attempt_token, owner, "completed")


@pytest.mark.integration
def test_claim_scan_bound_terminalizes_100_obsolete_rows_then_next_poll_advances(owned_jobs):
    base = database_clock() - timedelta(minutes=5)
    obsolete = [
        Job(
            kind="ping",
            logical_key=f"{owned_jobs.prefix}:obsolete:{n}",
            payload={},
            created_at=base + timedelta(seconds=n),
            attempts=3,
            progress=0.2,
        )
        for n in range(100)
    ]
    owned_jobs.add(*obsolete)
    valid = owned_jobs.new(created_at=base + timedelta(seconds=100))
    with worker_session() as owner:
        assert jobs.claim(owner) is None
        with transaction() as session:
            completed_scan = list(
                session.scalars(select(Job).where(Job.id.in_([row.id for row in obsolete])))
            )
        assert len(completed_scan) == 100
        assert all(
            row.status == "failed" and row.attempts == 3 and row.progress == 0.2 for row in completed_scan
        )
        assert read(valid.id).status == "queued" and read(valid.id).attempts == 0
        attempt = jobs.claim(owner)
        assert attempt.id == valid.id
        jobs.finish(attempt.id, attempt.attempt_token, owner, "completed")


@pytest.mark.integration
def test_lost_owned_worker_session_cannot_renew_job_and_primary_error_survives_cleanup(owned_jobs):
    row = owned_jobs.new()
    with pytest.raises(WorkerOwnershipLost, match="singleton session"), worker_session() as owner:
        attempt = jobs.claim(owner)
        original = stored(row.id)
        with transaction() as session:
            # Wait for this owned backend's exit, not merely signal delivery.
            assert session.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": owner.pid})
        jobs.checkpoint(row.id, attempt.attempt_token, owner, 1)
    assert stored(row.id) == original
