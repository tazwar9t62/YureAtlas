"""The job API preserves original requests and exposes validated execution stages."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from yureatlas import jobs
from yureatlas.api import app
from yureatlas.db import transaction
from yureatlas.tables import Job
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration


def test_forecast_api_pins_request_time_and_returns_original_execution_on_key_reuse(monkeypatch):
    clock = {"value": utc("2027-01-01T23:50:00+09:00")}
    monkeypatch.setattr(jobs, "now", lambda: clock["value"])
    headers = {"x-yureatlas-request": "1"}
    key = "api-execution-" + uuid4().hex
    with TestClient(app) as client:
        response = client.post("/forecasts", json={"key": key}, headers=headers)
        assert response.status_code == 202
        original = response.json()
        identity = original["id"]
        try:
            target = original["execution"]
            assert target["state"] == "target_pinned" and target["target_basis"] == "request_time"
            assert utc(target["request_time"]) == clock["value"]
            assert utc(target["input_cutoff"]) == clock["value"]
            assert utc(target["start"]) == clock["value"] + timedelta(minutes=30)
            assert original["payload"]["start"] is None
            assert original["payload"]["key"] == key
            clock["value"] += timedelta(days=1)
            repeated = client.post(
                "/forecasts", json={"key": key, "start": clock["value"].isoformat()}, headers=headers
            )
            assert repeated.status_code == 202
            assert repeated.json() == original
            assert client.get(f"/jobs/{identity}").json() == original
        finally:
            cancelled = client.post(f"/jobs/{identity}/cancel", headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["execution"] == original["execution"]
        assert cancelled.json()["payload"] == original["payload"]


def test_legacy_job_api_reports_absent_execution_without_inventing_resolution():
    with transaction() as session:
        job = Job(
            kind="forecast",
            payload={"legacy": True},
            logical_key="api-legacy-" + uuid4().hex,
            status="failed",
            attempts=2,
            error="Legacy fixture has no known execution identity",
        )
        session.add(job)
        session.flush()
    with TestClient(app) as client:
        response = client.get(f"/jobs/{job.id}")
        assert response.status_code == 200
        assert response.json()["execution"] is None
        assert response.json()["payload"] == {"legacy": True}
