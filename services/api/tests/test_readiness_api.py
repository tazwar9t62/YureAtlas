"""Public readiness failures stay actionable without exposing connection secrets."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from yureatlas import api, readiness
from yureatlas.db import transaction
from yureatlas.readiness_prerequisites import ReadinessPrerequisites
from yureatlas.schedule_schemas import ScheduleWorker
from yureatlas.tables import WorkerState
from yureatlas.timeutil import utc
from yureatlas.worker_locks import worker_session

CHECKED = utc("2026-10-25T12:00:00Z")
SECRET = "postgresql://researcher:private-password@private-host/research"


@pytest.fixture
def healthy_dependencies(monkeypatch):
    @contextmanager
    def connection():
        yield SimpleNamespace(scalar=lambda query: CHECKED)

    state = ScheduleWorker(
        status="fresh", heartbeat=CHECKED, owner_token="owned", environment_enabled=False, reason="Current"
    )
    monkeypatch.setattr(readiness, "readiness_session", connection)
    monkeypatch.setattr(
        readiness,
        "schema_status",
        lambda session: {
            "status": "ok",
            "code": "schema_current",
            "message": "Compatible",
            "expected": "010",
            "observed": ["010"],
        },
    )
    monkeypatch.setattr(
        readiness,
        "operational_status",
        lambda session, checked: {
            "worker": state,
            "scheduler": {"status": "ok", "code": "scheduler_environment_disabled", "message": "Disabled"},
        },
    )
    monkeypatch.setattr(
        readiness,
        "inspect_prerequisites",
        lambda session, checked: ReadinessPrerequisites(status="first_run", message="Create an experiment"),
    )
    monkeypatch.setattr(
        readiness,
        "probe_artifact_root",
        lambda path: {"status": "ok", "code": "artifact_probe_ok", "message": "Writable"},
    )
    return state


def test_first_run_is_advisory_and_readiness_is_never_cached(healthy_dependencies):
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ready" and body["prerequisites"]["status"] == "first_run"
    assert body["prerequisites"]["advisory"] is True
    assert body["checks"]["scheduler"]["code"] == "scheduler_environment_disabled"


def test_database_failure_is_structured_and_liveness_stays_independent(healthy_dependencies, monkeypatch):
    @contextmanager
    def unavailable():
        raise OperationalError("connect", {}, RuntimeError(SECRET))
        yield  # pragma: no cover

    monkeypatch.setattr(readiness, "readiness_session", unavailable)
    with TestClient(api.app) as client:
        response = client.get("/ready")
        assert client.get("/health").json() == {"status": "ok", "service": "YureAtlas"}
    assert response.status_code == 503 and SECRET not in response.text
    checks = response.json()["checks"]
    assert checks["database"]["code"] == "database_probe_failed"
    assert checks["schema"]["status"] == "skipped" and checks["artifacts"]["status"] == "ok"
    assert response.json()["prerequisites"]["status"] == "unavailable"


def test_incompatible_schema_skips_application_queries(healthy_dependencies, monkeypatch):
    monkeypatch.setattr(
        readiness,
        "schema_status",
        lambda session: {
            "status": "failed",
            "code": "schema_mismatch",
            "message": "Run migrations",
            "expected": "010",
            "observed": ["009"],
        },
    )
    monkeypatch.setattr(
        readiness, "operational_status", lambda *args: pytest.fail("Incompatible schema queried")
    )
    monkeypatch.setattr(
        readiness, "inspect_prerequisites", lambda *args: pytest.fail("Prerequisites queried")
    )
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["schema"]["observed"] == ["009"]
    assert response.json()["checks"]["worker"]["status"] == "skipped"


def test_artifact_failure_is_fatal_even_with_healthy_database(healthy_dependencies, monkeypatch):
    monkeypatch.setattr(
        readiness,
        "probe_artifact_root",
        lambda path: {
            "status": "failed",
            "code": "artifact_probe_cleanup_failed",
            "message": "Cleanup failed",
        },
    )
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] == "ok"
    assert response.json()["checks"]["artifacts"]["code"] == "artifact_probe_cleanup_failed"


def test_invalid_operational_metadata_fails_without_leaking_saved_values(healthy_dependencies, monkeypatch):
    def invalid(*args):
        raise ValueError(SECRET)

    monkeypatch.setattr(readiness, "operational_status", invalid)
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503 and SECRET not in response.text
    assert response.json()["checks"]["scheduler"]["code"] == "operational_state_invalid"


def test_invalid_prerequisite_metadata_remains_advisory(healthy_dependencies, monkeypatch):
    def invalid(*args):
        raise ValueError(SECRET)

    monkeypatch.setattr(readiness, "inspect_prerequisites", invalid)
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 200 and SECRET not in response.text
    assert response.json()["prerequisites"]["status"] == "unavailable"


def test_liveness_never_calls_dependency_probes(monkeypatch):
    monkeypatch.setattr(api, "inspect_readiness", lambda: pytest.fail("Liveness probed dependencies"))
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.integration
def test_actual_readiness_and_legacy_source_status_reject_stale_future_and_offline_worker(monkeypatch):
    monkeypatch.setattr(readiness.settings, "schedule_enabled", False)
    with TestClient(api.app) as client:
        with worker_session():
            ready = client.get("/ready")
            assert ready.status_code == 200, ready.text
            assert ready.json()["checks"]["artifacts"]["code"] == "artifact_probe_ok"
            assert ready.json()["checks"]["scheduler"]["code"] == "scheduler_environment_disabled"
            assert ready.json()["prerequisites"]["advisory"] is True
            assert client.get("/sources").json()["worker"]["online"] is True
            for delta, expected in ((-91, "stale"), (60, "clock_skew")):
                with transaction() as session:
                    clock = session.scalar(text("SELECT clock_timestamp()"))
                    session.get(WorkerState, "scheduler").heartbeat = clock + timedelta(seconds=delta)
                result = client.get("/ready")
                assert result.status_code == 503, result.text
                assert result.json()["checks"]["worker"]["worker"]["status"] == expected
                source_status = client.get("/sources").json()["worker"]
                assert source_status["status"] == expected and source_status["online"] is False
        offline = client.get("/ready")
        assert offline.status_code == 503
        assert offline.json()["checks"]["worker"]["worker"]["status"] == "offline"
        assert client.get("/sources").json()["worker"]["online"] is False
