"""Corrections change observations without rewriting a completed research archive."""

import csv
import io
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from yureatlas import ingestion, tasks
from yureatlas.archive import evaluate_forecast, issue_forecast, train_rate
from yureatlas.artifacts import canonical, put, read
from yureatlas.catalogue import ensure_experiment, load_experiment, revisions_asof
from yureatlas.db import transaction
from yureatlas.learning import train_suite
from yureatlas.providers import IMPORT_PARSER_VERSION, USGS
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import (
    Evaluation,
    EvaluationCell,
    FeatureSet,
    Forecast,
    ForecastCell,
    Ingestion,
    Model,
    Revision,
    Snapshot,
    now,
)
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration

START = utc("2021-07-01T15:00:00Z")  # Midnight JST; the forecast covers exactly one local day.
END = START + timedelta(days=1)
HISTORY_START = START - timedelta(days=160)
TRAINING_CUTOFF = START - timedelta(days=1)
VERSION = END + timedelta(days=1)


def catalogue_records():
    def record(identity, origin):
        return {
            "provider_id": identity,
            "origin_time": origin,
            "provider_updated": VERSION,
            "longitude": 140.5,
            "latitude": 35.5,
            "depth_km": 12,
            "magnitude": 4.2,
            "magnitude_type": "J",
            "status": "reviewed",
            "event_type": "earthquake",
        }

    return [record(f"history-{i}", START - timedelta(days=100 - i * 4)) for i in range(8)] + [
        record("observed", START + timedelta(hours=6))
    ]


def import_csv(experiment, records, *, namespace="catalogue", declared=True):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(catalogue_records()[0]))
    writer.writeheader()
    writer.writerows(records)
    return tasks.execute(
        "import",
        {
            "experiment_id": experiment.id,
            "start": HISTORY_START.isoformat(),
            "end": END.isoformat(),
            "namespace": namespace,
            "format": "csv",
            "artifact": put(output.getvalue().encode(), "csv"),
            "parser_version": f"csv-v{IMPORT_PARSER_VERSION}",
            "coverage_declared": declared,
        },
        lambda _: None,
    )


def freeze_archive(experiment, forecast, evaluation):
    """Read and later compare the exact original rows, including their artifact bytes."""
    queries = [
        (Model, Model.experiment_id == experiment.id),
        (FeatureSet, FeatureSet.experiment_id == experiment.id),
        (Forecast, Forecast.id == forecast.id),
        (ForecastCell, ForecastCell.forecast_id == forecast.id),
        (Evaluation, Evaluation.id == evaluation.id),
        (EvaluationCell, EvaluationCell.evaluation_id == evaluation.id),
    ]
    with transaction() as session:
        snapshots = list(session.scalars(select(Snapshot).where(Snapshot.source_id == experiment.source_id)))
        revision_ids = {identity for snapshot in snapshots for identity in snapshot.revision_ids}
        queries.extend(
            [
                (Snapshot, Snapshot.id.in_([snapshot.id for snapshot in snapshots])),
                (Revision, Revision.id.in_(revision_ids)),
            ]
        )
        refs = set()
        for model in session.scalars(select(Model).where(Model.experiment_id == experiment.id)):
            if model.artifact:
                refs.add(model.artifact)
        for feature_set in session.scalars(
            select(FeatureSet).where(FeatureSet.experiment_id == experiment.id)
        ):
            refs.add(feature_set.artifact)
            refs.update(json.loads(read(feature_set.artifact)))
        refs.update(session.scalars(select(Revision.raw_ref).where(Revision.id.in_(revision_ids))))
        if forecast.provenance.get("dependency_manifest"):
            refs.add(forecast.provenance["dependency_manifest"])

    def rows():
        with transaction() as session:
            return [
                canonical(
                    [
                        dict(row)
                        for row in session.execute(
                            select(table.__table__).where(condition).order_by(*table.__table__.primary_key)
                        ).mappings()
                    ]
                )
                for table, condition in queries
            ]

    original_rows = rows()
    original_bytes = {ref: read(ref) for ref in refs}

    def assert_unchanged():
        assert rows() == original_rows
        assert {ref: read(ref) for ref in refs} == original_bytes

    return assert_unchanged


def observations(evaluation):
    with transaction() as session:
        return {
            row.cell_id: row.observed_count
            for row in session.scalars(
                select(EvaluationCell).where(EvaluationCell.evaluation_id == evaluation.id)
            )
        }


@pytest.mark.parametrize(
    "correction",
    ["downgrade", "relocation", "moved_origin", "event_type", "deletion", "older", "replay", "omitted"],
)
def test_csv_corrections_append_observations_and_preserve_saved_research(correction):
    identity = "acceptance_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="jma",
            catalogue_namespace=identity,
            west=140,
            east=142,
            south=35,
            north=37,
        )
    )
    records = catalogue_records()
    initial_import = import_csv(experiment, records)
    models = train_suite(experiment.id, TRAINING_CUTOFF, fit_days=30, tune_days=7, calibration_days=7)
    model = next(model for model in models if model.algorithm == "historical_rate")
    assert model.status == "available"
    assert json.loads(read(model.metadata_["feature_manifest"]))  # Real saved Parquet feature batches.
    forecast = issue_forecast(experiment.id, model.id, START, mode="retrospective_revised")
    first = evaluate_forecast(forecast.id)
    assert first.status == "provisional" and first.version == 1
    assert first.metrics["positives"] == sum(observations(first).values()) == 1
    assert_unchanged = freeze_archive(experiment, forecast, first)
    before_correction = now()
    original_members = {row.id for row in revisions_asof(experiment.source_id, before_correction)}

    revised = [dict(record) for record in records]
    changes = {
        "downgrade": {"magnitude": 2.8},
        "relocation": {"longitude": 150},
        "moved_origin": {"origin_time": END + timedelta(days=1)},
        "event_type": {"event_type": "explosion"},
        "deletion": {"status": "deleted"},
        "older": {"magnitude": 2.8, "provider_updated": VERSION - timedelta(days=1)},
    }
    if correction in changes:
        # Correct both an observation and a fitted-history event. Earlier model
        # snapshots and materialized features must survive changes to their inputs.
        for record in (revised[0], revised[-1]):
            record.update({"provider_updated": VERSION + timedelta(days=1), **changes[correction]})
    elif correction == "omitted":
        revised.pop()  # An absent CSV row is never a deletion instruction.

    result = import_csv(experiment, revised)
    second = evaluate_forecast(forecast.id)
    changed_observations = correction in {"downgrade", "relocation", "moved_origin", "event_type", "deletion"}
    if changed_observations:
        assert result["added"] == 2
        assert second.id != first.id and second.version == 2
        assert second.status == "provisional"
        assert second.metrics["positives"] == sum(observations(second).values()) == 0
        with transaction() as session:
            assert (
                session.get(Snapshot, second.snapshot_id).revision_ids
                != session.get(Snapshot, first.snapshot_id).revision_ids
            )
    else:
        assert second.id == first.id
        assert second.metrics["positives"] == 1
        if correction == "older":
            assert result["added"] == 2  # Archived evidence, without superseding the newer provider version.
        elif correction == "replay":
            assert result == dict(initial_import, idempotent_replay=True)
        else:
            assert result["added"] == 0 and result["duplicate"] == 8

    assert {row.id for row in revisions_asof(experiment.source_id, before_correction)} == original_members
    assert_unchanged()
    assert issue_forecast(experiment.id, model.id, START, mode="retrospective_revised").id == forecast.id
    assert evaluate_forecast(forecast.id).id == second.id


@pytest.mark.parametrize("failure", ["partial", "failed"])
def test_unresolved_usgs_retrieval_removes_scoring_coverage_until_reconciled(monkeypatch, failure):
    # USGS sources share a retrieval domain, so use a distinct valid envelope to
    # isolate these scenarios from every other test's catalogue.
    west = 140 + int(uuid4().hex, 16) / 2**128 / 100
    experiment = ensure_experiment(
        ExperimentSpec(
            name="usgs_acceptance_" + uuid4().hex[:12],
            provider="usgs",
            west=west,
            east=142,
            south=35,
            north=37,
        )
    )
    records = catalogue_records()
    phase = "initial"

    def feature(record):
        return {
            "type": "Feature",
            "id": record["provider_id"],
            "geometry": {
                "type": "Point",
                "coordinates": [record["longitude"], record["latitude"], record["depth_km"]],
            },
            "properties": {
                "time": record["origin_time"].timestamp() * 1000,
                "updated": record["provider_updated"].timestamp() * 1000,
                "mag": record["magnitude"],
                "magType": "mb",
                "status": record["status"],
                "type": record["event_type"],
            },
        }

    def response(request):
        if phase == "failed":
            return httpx.Response(400, json={"error": "Fixture query failure"})
        if "eventid" in request.url.params:
            assert request.url.params["eventid"] == "observed"
            correction = dict(records[-1], magnitude=2.8, provider_updated=VERSION + timedelta(days=1))
            payload = feature(correction)
            if phase == "partial":
                payload["properties"]["mag"] = None
            return httpx.Response(200, json=payload)
        start, end = (utc(request.url.params[key]) for key in ("starttime", "endtime"))
        selected = [record for record in records if start <= record["origin_time"] < end]
        if phase != "initial":
            selected = []  # A filtered query cannot prove that the known event was deleted.
        if request.url.path.endswith("/count"):
            return httpx.Response(200, json={"count": len(selected)})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "metadata": {"count": len(selected)},
                "features": [feature(r) for r in selected],
            },
        )

    monkeypatch.setattr(
        ingestion,
        "USGS",
        lambda **kwargs: USGS(httpx.AsyncClient(transport=httpx.MockTransport(response)), **kwargs),
    )
    assert ingestion.ingest_usgs(experiment.id, HISTORY_START, END)["status"] == "complete"
    model = train_rate(experiment.id, TRAINING_CUTOFF, HISTORY_START)
    forecast = issue_forecast(experiment.id, model.id, START, mode="retrospective_revised")
    first = evaluate_forecast(forecast.id)
    assert first.status == "provisional" and first.metrics["positives"] == 1
    assert_unchanged = freeze_archive(experiment, forecast, first)
    phase = failure
    if failure == "failed":
        with pytest.raises(httpx.HTTPStatusError):
            ingestion.ingest_usgs(experiment.id, START, END)
    else:
        assert ingestion.ingest_usgs(experiment.id, START, END)["status"] == "partial"
    unknown = evaluate_forecast(forecast.id)
    assert unknown.version == 2 and unknown.status == "awaiting_data"
    assert observations(unknown) == {}  # Unknown intervals must not produce false negative labels.
    _, grid = load_experiment(experiment.id)
    assert unknown.metrics["missing"] == len(grid.ids)
    assert_unchanged()
    assert_unknown_unchanged = freeze_archive(experiment, forecast, unknown)
    with transaction() as session:
        receipt = session.scalar(
            select(Ingestion)
            .where(Ingestion.source_id == experiment.source_id)
            .order_by(Ingestion.created_at.desc())
            .limit(1)
        )
        assert receipt.status == failure
        assert receipt.report["requests"]
        assert all(read(request["raw_ref"]) for request in receipt.report["requests"])

    phase = "recovered"
    assert ingestion.ingest_usgs(experiment.id, START, END)["status"] == "complete"
    recovered = evaluate_forecast(forecast.id)
    assert recovered.version == 3 and recovered.status == "provisional"
    assert recovered.metrics["positives"] == sum(observations(recovered).values()) == 0
    assert_unchanged()
    assert_unknown_unchanged()
