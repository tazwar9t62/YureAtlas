"""FDSN query shape captured from the public-domain USGS service on 2026-10-25."""

import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from yureatlas import providers
from yureatlas.artifacts import read
from yureatlas.config import settings
from yureatlas.providers import USGS
from yureatlas.timeutil import utc

START, END = utc("2023-05-05T00:00:00Z"), utc("2023-05-06T00:00:00Z")
BOUNDS = {"west": 136, "east": 138, "south": 36, "north": 38}


@pytest.fixture
def payload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_dir", tmp_path)
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    return json.loads(Path(__file__).with_name("fixtures").joinpath("usgs_20230505_query.json").read_bytes())


def retrieve(payload, preflight=5):
    def response(request):
        return httpx.Response(
            200, json={"count": preflight} if request.url.path.endswith("/count") else payload
        )

    with closing(USGS(httpx.AsyncClient(transport=httpx.MockTransport(response)))) as provider:
        return list(provider.retrieve(START, END, BOUNDS, 4.0))


def test_live_query_without_metadata_count_uses_independent_preflight(payload):
    assert "count" not in payload["metadata"]
    [(start, end, records, report)] = retrieve(payload)
    assert (start, end) == (START, END)
    assert report["reported_count"] == len(records) == 5
    assert report["rejected_count"] == 0
    assert records[0]["provider_id"] == "us6000k9mb"
    assert json.loads(read(report["raw_ref"])) == payload


def test_missing_metadata_count_cannot_bypass_preflight_mismatch(payload):
    with pytest.raises(ValueError, match="count"):
        retrieve(payload, preflight=4)


@pytest.mark.parametrize("count", [None, True, 5.0, "5", 4])
def test_present_invalid_or_conflicting_metadata_count_is_never_ignored(payload, count):
    payload["metadata"]["count"] = count
    with pytest.raises(ValueError, match="count"):
        retrieve(payload)


def test_matching_metadata_count_is_still_supported(payload):
    payload["metadata"]["count"] = 5
    assert len(retrieve(payload)[0][2]) == 5


def test_feed_without_metadata_count_stays_invalid_without_a_preflight(payload):
    with (
        closing(
            USGS(
                httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
            )
        ) as provider,
        pytest.raises(ValueError, match="count"),
    ):
        provider.feed()
