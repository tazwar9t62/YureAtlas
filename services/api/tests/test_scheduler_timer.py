"""Scheduler wakeups and shutdown use local threads, never a database."""

import logging
import threading
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from yureatlas import scheduling, worker
from yureatlas.timeutil import utc


@pytest.fixture(autouse=True)
def short_enabled_timer(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "schedule_enabled", True)
    monkeypatch.setattr(scheduling, "RECONCILE_SECONDS", 0.001)


def test_initial_reconciliation_failure_logs_and_later_tick_recovers(monkeypatch, caplog):
    recovered = threading.Event()
    release = threading.Event()
    attempts = []

    def reconcile():
        attempts.append(threading.current_thread().name)
        if len(attempts) == 1:
            raise RuntimeError("Temporary scheduling database outage")
        recovered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release successful reconciliation")

    monkeypatch.setattr(scheduling, "reconcile", reconcile)
    with caplog.at_level(logging.ERROR, logger="yureatlas.scheduling"):
        timer = scheduling.start_scheduler()
        try:
            assert recovered.wait(5), "A failed startup prevented the later reconciliation tick"
            assert len(attempts) == 2
            assert attempts[0] == threading.current_thread().name
            assert attempts[1] == "yureatlas-scheduler"
            assert "Schedule reconciliation failed; durable cursor unchanged" in caplog.text
            assert "Temporary scheduling database outage" in caplog.text
        finally:
            timer.shutdown(wait=False)
            release.set()
            timer.shutdown(wait=True)
    assert not timer._thread.is_alive()
    assert len(attempts) == 2


def test_wait_ticks_continue_with_frozen_backward_and_forward_wall_clocks(monkeypatch):
    initial = utc("2025-01-01T23:39:00+09:00")
    clocks = [initial, initial - timedelta(days=5), initial - timedelta(days=5), initial + timedelta(days=60)]
    clock = {"value": clocks[0]}
    observed = []
    completed = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(scheduling, "now", lambda: clock["value"])

    def reconcile():
        observed.append(scheduling.now())
        if len(observed) < len(clocks):
            clock["value"] = clocks[len(observed)]
        else:
            completed.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the final clock check")

    monkeypatch.setattr(scheduling, "reconcile", reconcile)
    timer = scheduling.start_scheduler()
    try:
        assert completed.wait(5), "Wall-clock changes prevented the monotonic timer from waking"
    finally:
        timer.shutdown(wait=False)
        release.set()
        timer.shutdown(wait=True)
    assert observed == clocks
    assert not timer._thread.is_alive()


def test_worker_waits_for_active_reconciliation_before_releasing_singleton(monkeypatch):
    active = threading.Event()
    release_reconciliation = threading.Event()
    shutdown_entered = threading.Event()
    singleton_released = threading.Event()
    worker_done = threading.Event()
    order = []
    failures = []
    timers = []
    attempts = []
    primary_error = RuntimeError("Stop the worker after its first job check")

    @contextmanager
    def connect():
        order.append("singleton_acquired")
        try:
            yield SimpleNamespace(scalar=lambda _statement: True)
        finally:
            order.append("singleton_released")
            singleton_released.set()

    @contextmanager
    def transaction():
        yield SimpleNamespace(execute=lambda _statement: None)

    def reconcile():
        attempts.append(None)
        if len(attempts) == 1:
            return  # The immediate startup pass finishes normally.
        order.append("reconciliation_started")
        active.set()
        if not release_reconciliation.wait(5):
            raise RuntimeError("Test did not release active reconciliation")
        order.append("reconciliation_finished")

    original_start = scheduling.start_scheduler

    def start_scheduler():
        timer = original_start()
        timers.append(timer)
        original_shutdown = timer.shutdown

        def shutdown(wait=True):
            order.append("shutdown_entered")
            shutdown_entered.set()
            original_shutdown(wait=wait)
            order.append("shutdown_finished")

        timer.shutdown = shutdown
        return timer

    def run_once():
        if not active.wait(5):
            raise RuntimeError("Scheduler never began the concurrent reconciliation")
        raise primary_error

    def run_worker():
        try:
            worker.main()
        except RuntimeError as error:
            failures.append(error)
        finally:
            worker_done.set()

    monkeypatch.setattr(worker, "engine", SimpleNamespace(connect=connect))
    monkeypatch.setattr(worker, "transaction", transaction)
    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(scheduling, "reconcile", reconcile)
    monkeypatch.setattr(scheduling, "start_scheduler", start_scheduler)
    running_worker = threading.Thread(target=run_worker, daemon=True)
    running_worker.start()
    try:
        assert shutdown_entered.wait(5), "The failing worker did not request scheduler shutdown"
        assert not singleton_released.is_set()
        assert not worker_done.is_set()
        release_reconciliation.set()
        assert worker_done.wait(5), "Worker did not exit after the active scheduler pass completed"
    finally:
        release_reconciliation.set()
        running_worker.join(timeout=5)
        for timer in timers:
            timer.shutdown(wait=True)
    assert not running_worker.is_alive()
    assert failures == [primary_error]
    assert order[:6] == [
        "singleton_acquired",
        "reconciliation_started",
        "shutdown_entered",
        "reconciliation_finished",
        "shutdown_finished",
        "singleton_released",
    ]
    assert all(not timer._thread.is_alive() for timer in timers)
