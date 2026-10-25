"""Multipart import requests preserve effective parameters and retry identity."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from yureatlas import api
from yureatlas.timeutil import utc

FORM = {
    "experiment_id": "experiment",
    "start": "2020-01-01T00:00:00Z",
    "end": "2020-01-02T00:00:00Z",
    "namespace": "stable_file",
    "format": "csv",
}
RAW = b"provider_id,origin_time,longitude,latitude,magnitude,magnitude_type\na,2020-01-01T01:00:00Z,140,35,4,J\n"


@pytest.fixture
def requests(monkeypatch):
    state = SimpleNamespace(
        jobs=[],
        artifacts=[],
        experiment=SimpleNamespace(
            id="experiment",
            source_id="source",
            config_hash="spec",
            config={"provider": "jma", "catalogue_namespace": "collection"},
        ),
    )

    @contextmanager
    def fake_transaction():
        yield SimpleNamespace(get=lambda cls, identity: state.experiment)

    def put(raw, suffix):
        state.artifacts.append(raw)
        return hashlib.sha256(raw).hexdigest() + "." + suffix

    def enqueue(kind, payload, key):
        state.jobs.append((payload, key))
        return SimpleNamespace(
            id=key,
            kind=kind,
            status="queued",
            progress=0,
            attempts=0,
            error=None,
            result={},
            created_at=utc("2020-02-01T00:00:00Z"),
            cancel_requested=False,
        )

    monkeypatch.setattr(api, "transaction", fake_transaction)
    monkeypatch.setattr("yureatlas.artifacts.put", put)
    monkeypatch.setattr("yureatlas.jobs.enqueue", enqueue)
    with TestClient(api.app) as client:
        state.post = lambda **changes: client.post(
            "/imports",
            data=dict(FORM, **changes),
            files={"file": ("catalogue.csv", RAW, "text/csv")},
            headers={"X-YureAtlas-Request": "1"},
        )
        yield state


def test_identical_effective_request_is_idempotent_and_coverage_is_opt_in(requests):
    assert requests.post().status_code == 202
    assert (
        requests.post(start="2020-01-01T09:00:00+09:00", end="2020-01-02T09:00:00+09:00").status_code == 202
    )
    assert requests.jobs[0] == requests.jobs[1]
    payload, _ = requests.jobs[0]
    assert payload["coverage_declared"] is False
    assert payload["mapping"] == {}
    assert payload["parser_version"] == "csv-v2"


@pytest.mark.parametrize(
    "changes",
    [
        {"end": "2020-01-03T00:00:00Z"},
        {"format": "jma"},
        {"mapping": '{"id":"provider_id"}'},
        {"coverage_declared": "true"},
        {"namespace": "another_file"},
        {"submission_id": "explicit-retry-1"},
    ],
)
def test_changed_request_parameters_do_not_reuse_an_unrelated_job(requests, changes):
    assert requests.post().status_code == requests.post(**changes).status_code == 202
    assert requests.jobs[0][1] != requests.jobs[1][1]


def test_mapping_key_order_does_not_change_identity(requests):
    assert requests.post(mapping=json.dumps({"id": "provider_id", "when": "origin_time"})).status_code == 202
    assert requests.post(mapping=json.dumps({"when": "origin_time", "id": "provider_id"})).status_code == 202
    assert requests.jobs[0] == requests.jobs[1]


@pytest.mark.parametrize(
    "mapping",
    [
        "not json",
        "[]",
        "null",
        '{"id":5}',
        '{"id":"unsupported"}',
        '{"id":"provider_id","alias":"provider_id"}',
        '{"id":"provider_id","id":"origin_time"}',
    ],
)
def test_invalid_mapping_is_rejected_before_storage_or_queueing(requests, mapping):
    assert requests.post(mapping=mapping).status_code == 422
    assert not requests.jobs and not requests.artifacts


def test_jma_mapping_and_legacy_collections_are_rejected_before_storage(requests):
    assert requests.post(format="jma", mapping='{"id":"provider_id"}').status_code == 422
    requests.experiment.config.pop("catalogue_namespace")
    response = requests.post()
    assert response.status_code == 422
    assert "collection" in response.json()["detail"]
    assert not requests.jobs and not requests.artifacts


def test_source_and_specification_are_part_of_job_identity(requests):
    assert requests.post().status_code == 202
    requests.experiment.source_id = "isolated_source"
    assert requests.post().status_code == 202
    requests.experiment.config_hash = "different_specification"
    assert requests.post().status_code == 202
    assert len({key for _, key in requests.jobs}) == 3
