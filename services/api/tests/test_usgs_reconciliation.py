"""Bounded provider reconciliation, with real HTTP decoding and no network."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from yureatlas import ingestion, providers
from yureatlas.artifacts import read
from yureatlas.catalogue import ensure_experiment, ingest_records
from yureatlas.config import settings
from yureatlas.eligibility import eligible_cell
from yureatlas.grid import StudyGrid
from yureatlas.jobs import Cancelled
from yureatlas.providers import USGS, RetrievalBudgetExceeded
from yureatlas.schemas import ExperimentSpec
from yureatlas.timeutil import utc

START = utc("2020-01-01T00:00:00Z")
END = START + timedelta(days=1)
CONFIG = {"provider": "usgs", "input_magnitude": 3, "target_magnitude": 4}


def feature(identity="known", **changes):
    return {
        "type": "Feature",
        "id": identity,
        "geometry": {"type": "Point", "coordinates": [140.5, 35.5, None]},
        "properties": dict(
            {
                "time": START.timestamp() * 1000,
                "updated": END.timestamp() * 1000,
                "status": "reviewed",
                "type": "earthquake",
                "mag": 4.2,
                "magType": "mb",
            },
            **changes,
        ),
    }


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features), "metadata": {"count": len(features)}}


@pytest.fixture
def provider_factory(tmp_path, monkeypatch):
    clock = SimpleNamespace(seconds=0.0)
    instances = []
    monkeypatch.setattr(settings, "artifact_dir", tmp_path / "artifacts")
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock.seconds)
    monkeypatch.setattr(
        providers.time, "sleep", lambda seconds: setattr(clock, "seconds", clock.seconds + seconds)
    )

    def create(handler, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = USGS(client, **kwargs)
        instances.append(provider)
        provider.clock = clock
        return provider

    yield create
    for provider in instances:
        provider.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"mag": 2.8},
        {"type": "explosion"},
        {"status": "deleted"},
        {"time": (END + timedelta(days=2)).timestamp() * 1000},
    ],
)
def test_id_lookup_has_no_discovery_filters_and_archives_exact_evidence(provider_factory, changes):
    calls = []
    payload = feature(**changes)
    payload["geometry"]["coordinates"][0] = 150

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    provider = provider_factory(handler)
    row, report = provider.retrieve_id("known")
    assert dict(calls[0].url.params) == {"eventid": "known", "format": "geojson", "includedeleted": "true"}
    assert row["longitude"] == 150
    assert row["provider_id"] == "known"
    assert json.loads(read(report["raw_ref"])) == payload
    assert json.loads(read(report["requests"][0]["request_ref"]))["params"] == dict(calls[0].url.params)
    assert utc(report["received_at"]).tzinfo is not None
    assert provider.id_requests == 1
    assert 0 < calls[0].extensions["timeout"]["read"] <= 15


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_id",
        "other_id",
        "collection",
        "missing_updated",
        "missing_status",
        "unknown_status",
        "bad_geometry",
        "missing_magnitude",
        "bad_properties",
        "boolean_magnitude",
        "boolean_longitude",
        "boolean_latitude",
        "boolean_depth",
        "string_magnitude",
    ],
)
def test_malformed_or_wrong_identity_never_resolves_a_known_id(provider_factory, mutation):
    payload = feature()
    if mutation == "missing_id":
        payload.pop("id")
    elif mutation == "other_id":
        payload["id"] = "alias-without-verified-mapping"
    elif mutation == "collection":
        payload = collection(payload)
    elif mutation == "missing_updated":
        payload["properties"].pop("updated")
    elif mutation == "missing_status":
        payload["properties"].pop("status")
    elif mutation == "unknown_status":
        payload["properties"]["status"] = "unknown"
    elif mutation == "bad_geometry":
        payload["geometry"] = None
    elif mutation == "missing_magnitude":
        payload["properties"]["mag"] = None
    elif mutation == "bad_properties":
        payload["properties"] = []
    elif mutation in {"boolean_magnitude", "string_magnitude"}:
        payload["properties"]["mag"] = True if mutation == "boolean_magnitude" else "4.2"
    else:
        index = {"boolean_longitude": 0, "boolean_latitude": 1, "boolean_depth": 2}[mutation]
        payload["geometry"]["coordinates"][index] = True
    provider = provider_factory(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        provider.retrieve_id("known")
    assert provider.requests[0]["raw_ref"]


@pytest.mark.parametrize("status", [204, 404, 409])
def test_no_data_and_http_errors_do_not_fabricate_deletion(provider_factory, status):
    provider = provider_factory(lambda request: httpx.Response(status))
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        provider.retrieve_id("known")
    assert provider.id_requests == 1
    assert provider.requests[0]["status_code"] == status


def test_retries_count_against_physical_request_budget(provider_factory):
    provider = provider_factory(lambda request: httpx.Response(429), max_id_requests=2)
    with pytest.raises(RetrievalBudgetExceeded):
        provider.retrieve_id("known")
    assert provider.id_requests == len(provider.requests) == 2
    assert all(r["status_code"] == 429 for r in provider.requests)


def test_detail_active_time_budget_rejects_late_response_and_caps_timeout(provider_factory):
    calls = []

    def handler(request):
        calls.append(request)
        provider.clock.seconds += 1
        return httpx.Response(200, json=feature())

    provider = provider_factory(handler, id_seconds=1)
    with pytest.raises(RetrievalBudgetExceeded):
        provider.retrieve_id("known")
    assert 0 < calls[0].extensions["timeout"]["read"] < 1
    assert provider.requests[0]["raw_ref"]
    assert provider.id_elapsed >= 1
    with pytest.raises(RetrievalBudgetExceeded):
        provider.retrieve_id("known")
    assert provider.id_requests == 1


@pytest.fixture
def ingest_environment(monkeypatch):
    state = SimpleNamespace(known=[], overflow=False, receipts=[], order=[])
    grid = StudyGrid(west=140, east=142, south=35, north=37)
    monkeypatch.setattr(
        ingestion, "load_experiment", lambda _: (SimpleNamespace(source_id="source", config=CONFIG), grid)
    )

    def capture(*args):
        state.order.append("known")
        return state.known[:], state.overflow

    def persist(source, records, start, end, report, *, status, received_at=None, discovery_context=None):
        if discovery_context is not None:
            records = [
                r
                for r in records
                if r["provider_id"] in state.known or eligible_cell(r, grid, CONFIG) is not None
            ]
        state.order.append(report.get("phase", "supplement"))
        state.receipts.append(
            {
                "records": records,
                "start": start,
                "end": end,
                "report": report,
                "status": status,
                "received_at": received_at,
            }
        )
        return {
            "added": len(records),
            "duplicate": 0,
            "persisted_provider_ids": [r["provider_id"] for r in records],
        }

    monkeypatch.setattr(ingestion, "known_ids_for_interval", capture)
    monkeypatch.setattr(ingestion, "ingest_records", persist)
    return state


def wire_provider(monkeypatch, provider):
    def create(**kwargs):
        provider.progress = kwargs["progress"]
        return provider

    monkeypatch.setattr(ingestion, "USGS", create)


def test_only_missing_preexisting_ids_are_refreshed_before_complete_coverage(
    provider_factory, ingest_environment, monkeypatch
):
    state = ingest_environment
    state.known = ["matched", "missing"]
    detail_ids = []

    def handler(request):
        state.order.append("request")
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 2})
        if "eventid" in request.url.params:
            detail_ids.append(request.url.params["eventid"])
            return httpx.Response(200, json=feature("missing", mag=2.8))
        return httpx.Response(200, json=collection(feature("matched"), feature("new")))

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    result = ingestion.ingest_usgs("experiment", START, END)
    assert state.order[:3] == ["started", "known", "request"]
    assert detail_ids == ["missing"]
    assert result["status"] == state.receipts[-1]["status"] == "complete"
    assert state.receipts[-1]["report"]["resolved_known_ids"] == 2
    assert all(r["report"]["coverage_effect"] == "supplement" for r in state.receipts if r["records"])
    correction = next(r for r in state.receipts if r["report"].get("known_id"))
    assert correction["records"][0]["magnitude"] == 2.8
    assert correction["received_at"] == utc(correction["report"]["received_at"])


def test_many_known_events_resolved_by_discovery_do_not_consume_id_budget(
    provider_factory, ingest_environment, monkeypatch
):
    state = ingest_environment
    state.known = [f"event-{i}" for i in range(150)]
    provider = provider_factory(
        lambda request: httpx.Response(
            200,
            json=(
                {"count": 150}
                if request.url.path.endswith("/count")
                else collection(*(feature(i) for i in state.known))
            ),
        )
    )
    wire_provider(monkeypatch, provider)
    assert ingestion.ingest_usgs("experiment", START, END)["status"] == "complete"
    assert provider.id_requests == 0


@pytest.mark.parametrize("outcome", ["missing", "malformed", "timeout", "budget", "overflow"])
def test_unresolved_or_uncounted_known_ids_keep_coverage_partial(
    provider_factory, ingest_environment, monkeypatch, outcome
):
    state = ingest_environment
    state.known = ["a", "b"] if outcome == "budget" else ["a"]
    state.overflow = outcome == "overflow"

    def handler(request):
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0})
        if "eventid" not in request.url.params:
            return httpx.Response(204)
        if outcome == "missing":
            return httpx.Response(404)
        if outcome == "malformed":
            return httpx.Response(200, json={})
        if outcome == "timeout":
            raise httpx.ReadTimeout("socket stalled", request=request)
        return httpx.Response(200, json=feature(request.url.params["eventid"], status="deleted"))

    provider = provider_factory(handler, max_id_requests=1 if outcome == "budget" else 100)
    wire_provider(monkeypatch, provider)
    result = ingestion.ingest_usgs("experiment", START, END)
    assert result["status"] == "partial"
    assert not any(r["status"] == "complete" for r in state.receipts)
    report = state.receipts[-1]["report"]
    if outcome in {"budget", "overflow"}:
        assert report["pending_ids_at_least"] == 1
        assert report["next_action"]
    else:
        assert report["failed_ids"] == 1
        assert not any(r["records"] for r in state.receipts)


@pytest.mark.parametrize("bad_response", ["no_features", "count_changed", "missing_updated"])
def test_discovery_never_converts_malformed_data_to_empty_complete_coverage(
    provider_factory, ingest_environment, monkeypatch, bad_response
):
    def handler(request):
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0 if bad_response == "no_features" else 1})
        if bad_response == "no_features":
            return httpx.Response(200, json={})
        if bad_response == "count_changed":
            return httpx.Response(200, json=collection())
        row = feature()
        row["properties"].pop("updated")
        return httpx.Response(200, json=collection(row))

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    if bad_response == "missing_updated":
        assert ingestion.ingest_usgs("experiment", START, END)["status"] == "partial"
    else:
        with pytest.raises(ValueError):
            ingestion.ingest_usgs("experiment", START, END)
        assert ingest_environment.receipts[-1]["status"] == "failed"
    assert not any(r["status"] == "complete" for r in ingest_environment.receipts)


def test_cancellation_preserves_unknown_attempt_and_propagates(
    provider_factory, ingest_environment, monkeypatch
):
    provider = provider_factory(lambda request: pytest.fail("No request should start after cancellation"))
    wire_provider(monkeypatch, provider)

    def cancel(progress):
        assert ingest_environment.receipts[0]["status"] == "partial"
        raise Cancelled("cancel")

    with pytest.raises(Cancelled, match="cancel"):
        ingestion.ingest_usgs("experiment", START, END, progress=cancel)
    assert ingest_environment.receipts[-1]["status"] == "partial"


def test_failure_recording_error_does_not_replace_provider_error(
    provider_factory, ingest_environment, monkeypatch
):
    provider = provider_factory(lambda request: httpx.Response(400))
    wire_provider(monkeypatch, provider)
    real_persist = ingestion.ingest_records

    def persist(*args, **kwargs):
        if args[4].get("phase") == "interrupted":
            raise RuntimeError("database disappeared")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(ingestion, "ingest_records", persist)
    with pytest.raises(httpx.HTTPStatusError) as error:
        ingestion.ingest_usgs("experiment", START, END)
    assert "Could not record failed ingestion" in error.value.__notes__[0]


@pytest.mark.parametrize("preexisting", [False, True])
def test_feed_is_supplemental_and_does_not_replace_known_id_reconciliation(
    provider_factory, ingest_environment, monkeypatch, preexisting
):
    state = ingest_environment
    state.known = ["known"] if preexisting else []
    detail_ids = []

    def handler(request):
        if "summary/" in request.url.path:
            assert "known" in state.order
            return httpx.Response(200, json=collection(feature()))
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0})
        if "eventid" in request.url.params:
            detail_ids.append(request.url.params["eventid"])
            return httpx.Response(200, json=feature(type="explosion"))
        return httpx.Response(204)

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    assert ingestion.ingest_usgs("experiment", START, END, use_feed=True)["status"] == "complete"
    assert detail_ids == ["known"]
    feed = next(r for r in state.receipts if r["report"].get("feed"))
    assert feed["status"] == "partial"
    assert feed["report"]["coverage_effect"] == "supplement"
    assert feed["received_at"] == utc(feed["report"]["received_at"])


def test_cancellation_during_id_request_is_not_an_individual_lookup_failure(
    provider_factory, ingest_environment, monkeypatch
):
    state = ingest_environment
    state.known = ["known"]
    state.cancel = False

    def handler(request):
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0})
        if "eventid" in request.url.params:
            state.cancel = True
            return httpx.Response(200, json=feature(status="deleted"))
        return httpx.Response(204)

    def progress(_fraction):
        if state.cancel:
            raise Cancelled("cancel during ID response")

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    with pytest.raises(Cancelled, match="during ID"):
        ingestion.ingest_usgs("experiment", START, END, progress=progress)
    assert state.receipts[-1]["report"]["phase"] == "interrupted"
    assert state.receipts[-1]["status"] == "partial"
    assert state.receipts[-1]["report"]["requests"][-1]["raw_ref"]
    assert not any(r["records"] for r in state.receipts)


def test_feed_id_overflow_cannot_claim_complete_coverage(provider_factory, ingest_environment, monkeypatch):
    monkeypatch.setattr(ingestion, "KNOWN_ID_LIMIT", 1)

    def handler(request):
        if "summary/" in request.url.path:
            return httpx.Response(200, json=collection(feature("a"), feature("b")))
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0})
        if "eventid" in request.url.params:
            return httpx.Response(200, json=feature(request.url.params["eventid"], mag=2.0))
        return httpx.Response(204)

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    result = ingestion.ingest_usgs("experiment", START, END, use_feed=True)
    assert result["status"] == "partial"
    assert ingest_environment.receipts[-1]["report"]["known_id_overflow"]


def test_cleanup_error_does_not_replace_cancellation(provider_factory, ingest_environment, monkeypatch):
    provider = provider_factory(lambda request: pytest.fail("No network expected"))
    wire_provider(monkeypatch, provider)
    original_close = provider.close

    def close():
        original_close()
        raise RuntimeError("transport close failed")

    def cancel(_):
        raise Cancelled("primary cancellation")

    monkeypatch.setattr(provider, "close", close)
    with pytest.raises(Cancelled, match="primary cancellation") as error:
        ingestion.ingest_usgs("experiment", START, END, progress=cancel)
    assert "Could not close USGS provider" in error.value.__notes__[0]
    monkeypatch.setattr(provider, "close", original_close)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_total_deadline_cancels_trickling_transport_and_closes_stream(tmp_path, monkeypatch, phase):
    # Real event-loop time proves the bound independently of the fake clock used
    # by retry tests. A never-completing header receive must be cancellable too.
    monkeypatch.setattr(settings, "artifact_dir", tmp_path / "artifacts")
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    state = SimpleNamespace(closed=False, cancelled=False, chunks=0)

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                state.chunks += 1
                yield b" "

        async def aclose(self):
            state.closed = True

    async def handler(request):
        if phase == "headers":
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                state.cancelled = True
                raise
        return httpx.Response(200, stream=Trickle())

    provider = USGS(httpx.AsyncClient(transport=httpx.MockTransport(handler)), id_seconds=0.5)
    try:
        with pytest.raises(RetrievalBudgetExceeded):
            provider.retrieve_id("known")
        assert provider.id_requests == 1
        assert 0.4 <= provider.id_elapsed < 2
        assert provider.requests[0]["error"] == "RetrievalBudgetExceeded"
        if phase == "body":
            assert state.closed and state.chunks > 1
            assert provider.requests[0]["response_complete"] is False
            assert read(provider.requests[0]["raw_ref"]) == b" " * state.chunks
        else:
            assert state.cancelled
    finally:
        provider.close()


@pytest.mark.parametrize("error_type", [Cancelled, httpx.ReadError, asyncio.CancelledError, None])
def test_response_artifact_failure_preserves_primary_error_but_is_otherwise_fatal(
    provider_factory, monkeypatch, error_type
):
    primary = error_type("primary failure") if error_type is not None else None
    original_put = providers.put

    def put(data, extension):
        if data == b"partial response":
            raise OSError("artifact disk unavailable")
        return original_put(data, extension)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial response"
            if primary is not None:
                raise primary

    monkeypatch.setattr(providers, "put", put)
    provider = provider_factory(lambda request: httpx.Response(200, stream=Body()))
    with pytest.raises(error_type or OSError) as error:
        provider.retrieve_id("known")
    if primary is not None:
        assert error.value is primary
        assert "Could not archive partial USGS response: OSError" in primary.__notes__
    else:
        assert str(error.value) == "artifact disk unavailable"
    assert provider.requests
    for attempt in provider.requests:
        assert attempt["artifact_error"] == "OSError"
        assert attempt["response_complete"] is (primary is None)
        assert "raw_ref" not in attempt
        assert utc(attempt["received_at"]).tzinfo is not None


@pytest.mark.integration
def test_known_id_capture_includes_historical_positions_and_deletions_with_bounded_overflow():
    experiment = ensure_experiment(ExperimentSpec(name="known_ids_" + uuid4().hex[:12], provider="synthetic"))
    rows = [
        {
            "provider_id": name,
            "origin_time": START if name != "outside" else START - timedelta(days=2),
            "provider_updated": START,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": None,
            "magnitude": 4.2,
            "magnitude_type": "synthetic",
            "status": "reviewed",
        }
        for name in ("moved", "deleted", "outside")
    ]
    ingest_records(experiment.source_id, rows, START, END, {}, received_at=END)
    ingest_records(
        experiment.source_id,
        [
            dict(rows[0], origin_time=END + timedelta(days=10), provider_updated=END),
            dict(rows[1], status="deleted", provider_updated=END),
        ],
        START,
        END,
        {},
        received_at=END + timedelta(days=1),
    )
    assert ingestion.known_ids_for_interval(experiment.source_id, START, END) == (["deleted", "moved"], False)
    assert ingestion.known_ids_for_interval(experiment.source_id, START, END, limit=1) == (["deleted"], True)


def test_unselected_feed_events_do_not_pollute_required_reconciliation_ids(
    provider_factory, ingest_environment, monkeypatch
):
    state = ingest_environment

    def handler(request):
        if "summary/" in request.url.path:
            outside = feature("unrelated")
            outside["geometry"]["coordinates"][0] = 150
            return httpx.Response(200, json=collection(outside))
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": 0})
        assert "eventid" not in request.url.params
        return httpx.Response(204)

    provider = provider_factory(handler)
    wire_provider(monkeypatch, provider)
    assert ingestion.ingest_usgs("experiment", START, END, use_feed=True)["status"] == "complete"
    assert provider.id_requests == 0
    assert not any(receipt["records"] for receipt in state.receipts)
