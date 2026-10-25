"""Real PostgreSQL session ownership covers an entire USGS reconciliation attempt."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from threading import Event as ThreadEvent
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from yureatlas import ingestion, source_locks
from yureatlas.catalogue import has_coverage, ingest_records
from yureatlas.db import engine, transaction
from yureatlas.grid import StudyGrid
from yureatlas.jobs import Cancelled
from yureatlas.source_locks import SourceAttemptBusy, SourceAttemptLost, usgs_attempt
from yureatlas.tables import Event, Ingestion, Source
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration
START = utc("2020-01-01T00:00:00Z")
END = START + timedelta(days=1)
CONFIG = {"provider": "usgs", "input_magnitude": 3, "target_magnitude": 4}


@pytest.fixture
def sources(monkeypatch):
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    with transaction() as session:
        rows = [
            Source(name="attempt_" + uuid4().hex, provider="usgs", attribution="test", details={})
            for _ in range(2)
        ]
        session.add_all(rows)
        session.flush()
        ids = [row.id for row in rows]
    monkeypatch.setattr(
        ingestion,
        "load_experiment",
        lambda identity: (SimpleNamespace(source_id=identity, config=CONFIG), grid),
    )
    return ids


def counts(source):
    with transaction() as session:
        return (
            session.scalar(select(func.count()).select_from(Ingestion).where(Ingestion.source_id == source)),
            session.scalar(select(func.count()).select_from(Event).where(Event.source_id == source)),
        )


class EmptyProvider:
    def __init__(self, before_retrieve=lambda: None, **kwargs):
        self.requests = []
        self.before_retrieve = before_retrieve
        self.closed = False

    def retrieve(self, start, end, *_args):
        self.before_retrieve()
        yield start, end, [], {"rejected_count": 0, "received_at": END.isoformat()}

    def budget_report(self):
        return {}

    def close(self):
        self.closed = True


def test_same_source_has_one_winner_without_network_work_or_receipts_from_loser(sources, monkeypatch):
    source = sources[0]
    in_network, release = ThreadEvent(), ThreadEvent()
    providers = []

    def wait():
        in_network.set()
        assert release.wait(timeout=10)

    def create(**kwargs):
        provider = EmptyProvider(wait, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(ingestion, "USGS", create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(ingestion.ingest_usgs, source, START, END)
        try:
            assert in_network.wait(timeout=10)
            before = counts(source)
            assert before == (1, 0)
            with pytest.raises(SourceAttemptBusy, match="retry"):
                ingestion.ingest_usgs(source, START, END)
            assert len(providers) == 1
            assert counts(source) == before
            assert not has_coverage(source, START, END)
            # Network work owns an idle session, not a transaction or row lock.
            with transaction() as session:
                state, transaction_start = session.execute(
                    text("""
                        SELECT activity.state, activity.xact_start
                        FROM pg_stat_activity activity JOIN pg_locks locks ON locks.pid = activity.pid
                        WHERE locks.locktype='advisory' AND locks.granted AND locks.objsubid=2
                          AND locks.classid::bigint=:namespace AND locks.objid::bigint=:key
                    """),
                    {
                        "namespace": source_locks.USGS_ATTEMPT_NAMESPACE,
                        "key": source_locks.attempt_key(source) % (1 << 32),
                    },
                ).one()
            assert state == "idle" and transaction_start is None
            with pytest.raises(SourceAttemptBusy):
                ingest_records(source, [], START, END, {}, status="complete")
        finally:
            release.set()
        assert winner.result(timeout=15)["status"] == "complete"
    assert providers[0].closed
    assert counts(source) == (3, 0)
    assert has_coverage(source, START, END)
    with usgs_attempt(source):
        pass  # The completed owner released the session mutex.


def test_disjoint_sources_can_retrieve_at_the_same_time(sources, monkeypatch):
    both_retrieving = Barrier(2)
    monkeypatch.setattr(
        ingestion, "USGS", lambda **kwargs: EmptyProvider(lambda: both_retrieving.wait(timeout=10))
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingestion.ingest_usgs, source, START, END) for source in sources]
        assert [future.result(timeout=20)["status"] for future in futures] == ["complete", "complete"]
    assert all(has_coverage(source, START, END) for source in sources)


@pytest.mark.parametrize("failure", [Cancelled, RuntimeError])
def test_interrupted_attempt_releases_mutex_and_keeps_coverage_unknown(sources, monkeypatch, failure):
    source = sources[0]
    ingest_records(source, [], START, END, {}, status="complete")

    def fail():
        raise failure("interrupted provider")

    provider = EmptyProvider(fail)
    monkeypatch.setattr(ingestion, "USGS", lambda **kwargs: provider)
    with pytest.raises(failure, match="interrupted provider"):
        ingestion.ingest_usgs(source, START, END)
    assert provider.closed
    assert not has_coverage(source, START, END)
    assert counts(source) == (3, 0)
    with usgs_attempt(source):
        pass


@pytest.mark.parametrize("primary", [None, Cancelled("primary cancellation")])
def test_unlock_failure_discards_physical_session_without_masking_primary(sources, monkeypatch, primary):
    source = sources[0]
    checked_out = engine.pool.checkedout()

    def fail_release(*_args):
        raise RuntimeError("unlock transport failed")

    with monkeypatch.context() as patch:
        patch.setattr(source_locks, "release_attempt", fail_release)
        with pytest.raises(type(primary) if primary else RuntimeError) as error, usgs_attempt(source):
            if primary:
                raise primary
        if primary:
            assert error.value is primary
            assert "USGS attempt cleanup failed: RuntimeError" in primary.__notes__
        else:
            assert "unlock transport failed" in str(error.value)
    assert engine.pool.checkedout() == checked_out
    with usgs_attempt(source):
        pass  # Failed unlock did not return a held advisory lock to the pool.


def test_disconnected_owner_cannot_write_after_a_successor_completes(sources):
    source = sources[0]
    old_context = usgs_attempt(source)
    old = old_context.__enter__()
    try:
        ingest_records(source, [], START, END, {"phase": "started"}, status="partial", attempt_owner=old)
        with transaction() as session:
            assert session.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": old.pid})
        with usgs_attempt(source) as successor:
            ingest_records(
                source, [], START, END, {"phase": "started"}, status="partial", attempt_owner=successor
            )
            ingest_records(
                source, [], START, END, {"phase": "finished"}, status="complete", attempt_owner=successor
            )
        assert has_coverage(source, START, END)
        before = counts(source)
        for status, report in [("partial", {"coverage_effect": "supplement"}), ("complete", {})]:
            with pytest.raises(SourceAttemptLost):
                ingest_records(source, [], START, END, report, status=status, attempt_owner=old)
        assert counts(source) == before
        assert has_coverage(source, START, END)
    finally:
        with pytest.raises(DBAPIError):
            old_context.__exit__(None, None, None)
    with usgs_attempt(source):
        pass


def test_stale_network_attempt_cannot_publish_after_session_loss_and_successor_completion(
    sources, monkeypatch
):
    source = sources[0]
    in_network, release = ThreadEvent(), ThreadEvent()
    created = []

    class StaleProvider(EmptyProvider):
        def retrieve(self, start, end, *_args):
            in_network.set()
            assert release.wait(timeout=10)
            yield (
                start,
                end,
                [
                    {
                        "provider_id": "stale-positive",
                        "origin_time": START,
                        "provider_updated": END,
                        "longitude": 140.5,
                        "latitude": 35.5,
                        "depth_km": None,
                        "magnitude": 4.2,
                        "magnitude_type": "mb",
                        "status": "reviewed",
                    }
                ],
                {"rejected_count": 0, "received_at": END.isoformat()},
            )

    def create(**kwargs):
        provider = StaleProvider() if not created else EmptyProvider()
        created.append(provider)
        return provider

    monkeypatch.setattr(ingestion, "USGS", create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(ingestion.ingest_usgs, source, START, END)
        try:
            assert in_network.wait(timeout=10)
            with transaction() as session:
                pid = session.scalar(
                    text("""
                    SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted AND objsubid=2
                      AND classid::bigint=:namespace AND objid::bigint=:key
                """),
                    {
                        "namespace": source_locks.USGS_ATTEMPT_NAMESPACE,
                        "key": source_locks.attempt_key(source) % (1 << 32),
                    },
                )
                assert session.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})
            assert ingestion.ingest_usgs(source, START, END)["status"] == "complete"
            before = counts(source)
        finally:
            release.set()
        with pytest.raises(SourceAttemptLost) as error:
            stale.result(timeout=15)
    assert "Could not record failed ingestion" in error.value.__notes__[0]
    assert counts(source) == before == (4, 0)
    assert has_coverage(source, START, END)
    assert all(provider.closed for provider in created)
