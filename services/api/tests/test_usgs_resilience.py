"""HTTP retries retain failed-attempt evidence and cannot silently resolve an ID."""

import asyncio
import json
from contextlib import closing

import httpx
import pytest
from yureatlas import providers
from yureatlas.artifacts import read
from yureatlas.config import settings
from yureatlas.providers import USGS, RetrievalBudgetExceeded


@pytest.fixture(autouse=True)
def isolated_artifacts_and_no_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_dir", tmp_path / "artifacts")
    monkeypatch.setattr(providers.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failures_exhaust_bounded_retries_with_request_evidence(error_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("fixture transport failure", request=request)

    with (
        closing(USGS(httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as provider,
        pytest.raises(error_type),
    ):
        provider.retrieve_id("known")
    assert len(calls) == provider.id_requests == len(provider.requests) == 4
    for number, attempt in enumerate(provider.requests, 1):
        assert attempt["attempt"] == number
        assert attempt["error"] == error_type.__name__
        assert "raw_ref" not in attempt  # No response body was received.
        assert json.loads(read(attempt["request_ref"]))["params"]["eventid"] == "known"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_exhausted_http_retries_preserve_every_response_body(status):
    body = b"temporary service failure"
    with (
        closing(
            USGS(
                httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body))
                )
            )
        ) as provider,
        pytest.raises(httpx.HTTPStatusError),
    ):
        provider.retrieve_id("known")
    assert provider.id_requests == len(provider.requests) == 4
    assert all(attempt["status_code"] == status for attempt in provider.requests)
    assert all(read(attempt["raw_ref"]) == body for attempt in provider.requests)


def test_successful_retry_retains_failed_attempt_and_only_uses_received_revision():
    calls = []
    payload = {
        "type": "Feature",
        "id": "known",
        "geometry": {"type": "Point", "coordinates": [140, 35, None]},
        "properties": {
            "time": 1700000000000,
            "updated": 1700000001000,
            "status": "reviewed",
            "type": "earthquake",
            "mag": 2.8,
            "magType": "mb",
        },
    }

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=payload)

    with closing(USGS(httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as provider:
        record, report = provider.retrieve_id("known")
    assert provider.id_requests == len(report["requests"]) == 2
    assert report["requests"][0]["error"] == "ReadTimeout"
    assert report["requests"][1]["status_code"] == 200
    assert report["received_at"] == report["requests"][1]["received_at"]
    assert json.loads(read(report["raw_ref"])) == payload
    assert record["magnitude"] == 2.8


def test_total_deadline_cancels_transport_before_response_headers_arrive():
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(204)

    with (
        closing(USGS(httpx.AsyncClient(transport=httpx.MockTransport(handler)), id_seconds=0.35)) as provider,
        pytest.raises(RetrievalBudgetExceeded),
    ):
        provider.retrieve_id("known")
    assert cancelled == [True]
    assert provider.id_requests == 1
    assert len(provider.requests) == 1
    assert provider.requests[0]["error"]


def test_protocol_failure_records_attempt_without_retry_or_fabricated_response():
    def handler(request):
        raise httpx.RemoteProtocolError("fixture disconnected headers", request=request)

    with (
        closing(USGS(httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as provider,
        pytest.raises(httpx.RemoteProtocolError),
    ):
        provider.retrieve_id("known")
    assert provider.id_requests == len(provider.requests) == 1
    attempt = provider.requests[0]
    assert attempt["error"] == "RemoteProtocolError"
    assert attempt["params"]["eventid"] == "known"
    assert json.loads(read(attempt["request_ref"]))["url"] == attempt["url"]
    assert "raw_ref" not in attempt
