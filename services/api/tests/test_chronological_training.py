"""Real bounded fits establish software chronology, not predictive performance.

Each suite fits 600 cell-days using real rate/HGB/logistic/hybrid estimators.
Separate catalogue identities defeat the training cache. ETAS has fewer than
40 fitting events and is naturally unavailable; its mathematics is tested apart.
"""

import csv
import hashlib
import io
import json
from datetime import timedelta
from itertools import pairwise
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sqlalchemy import select
from threadpoolctl import threadpool_limits
from yureatlas import archive, learning, tasks
from yureatlas.artifacts import put, read
from yureatlas.catalogue import ensure_experiment, ingest_records, load_experiment
from yureatlas.db import transaction
from yureatlas.features import NAMES, features
from yureatlas.rate import HistoricalRate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import (
    Evaluation,
    EvaluationCell,
    FeatureSet,
    Forecast,
    ForecastCell,
    Model,
    Revision,
    Snapshot,
)
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration
START = utc("2024-07-15T15:00:00Z")  # Midnight JST; all training labels cover full local days.
CUTOFF = START - timedelta(days=1)
FIT_START = CUTOFF - timedelta(days=50)
TUNE_START = CUTOFF - timedelta(days=20)
CAL_START = CUTOFF - timedelta(days=10)
WARM_START = FIT_START - timedelta(days=90, minutes=30)
ALGORITHMS = ("historical_rate", "hist_gradient_boosting", "calibrated_hybrid")


@pytest.fixture(autouse=True)
def bounded_numerical_threads():
    with threadpool_limits(limits=2):
        yield


def make_case(*, changed_test=False, sparse_calibration=False):
    identity = "chronology_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="jma",
            catalogue_namespace=identity,
            west=140,
            east=141,
            south=35,
            north=36,
        )
    )
    _, grid = load_experiment(experiment.id)
    assert len(grid.ids) == 12
    records = []

    def add(identity, origin, cell, magnitude=4.2):
        longitude, latitude = grid.lonlat[cell]
        records.append(
            {
                "provider_id": identity,
                "origin_time": origin,
                "provider_updated": START + timedelta(days=7),
                "longitude": float(longitude),
                "latitude": float(latitude),
                "depth_km": 12,
                "magnitude": magnitude,
                "magnitude_type": "J",
                "status": "reviewed",
                "event_type": "earthquake",
            }
        )

    for day in range(50):
        begin = FIT_START + timedelta(days=day)
        active = (day % 3,) if day < 30 else (0, 1) if day < 40 else (0, 2)
        if sparse_calibration and day >= 40:
            active = (0,)
        for cell in active:
            add(f"training-{day}-{cell}", begin + timedelta(hours=12), cell)
    for name, boundary in (("fit", FIT_START), ("tune", TUNE_START), ("calibration", CAL_START)):
        add(f"before-{name}", boundary - timedelta(seconds=1), 3)
        add(f"at-{name}", boundary, 4)
    add("last-calibration-second", CUTOFF - timedelta(seconds=1), 5)
    add("at-training-cutoff", CUTOFF, 6)
    # The first target begins after a full-day gap. Its preceding observations
    # are shared; only outcomes at/after START differ between the two sources.
    for cell in (0, 2):
        add(f"gap-{cell}", CUTOFF + timedelta(hours=12), cell)
    for label, target in (("first-feature", FIT_START), ("first-forecast", START)):
        feature_cutoff = target - timedelta(minutes=30)
        add(f"before-{label}-cutoff", feature_cutoff - timedelta(seconds=1), 10, magnitude=3.2)
        add(f"at-{label}-cutoff", feature_cutoff, 11, magnitude=3.2)
    for day in range(4):
        active = (8, 9) if changed_test else (0, 1)
        for offset, cell in enumerate(active):
            # An exact target-start event must count as an outcome, not input.
            add(f"held-out-{day}-{offset}", START + timedelta(days=day, hours=offset * 12), cell)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows(records)
    raw = output.getvalue().encode()
    artifact = put(raw, "csv")
    ingest_records(
        experiment.source_id,
        records,
        WARM_START,
        START + timedelta(days=4),
        {"raw_ref": artifact},
        received_at=START + timedelta(days=8),
        import_identity={
            "catalogue_namespace": identity,
            "namespace": "catalogue",
            "format": "csv",
            "artifact": artifact,
            "line_count": len(raw.splitlines()),
            "mapping": {},
            "parser_version": "csv-v2",
            "coverage_declared": True,
        },
    )
    return SimpleNamespace(experiment=experiment, grid=grid, records=records)


def fit(case):
    return {
        model.algorithm: model
        for model in learning.train_suite(
            case.experiment.id, CUTOFF, fit_days=30, tune_days=10, calibration_days=10
        )
    }


def verified_bytes(ref):
    raw = read(ref)
    assert hashlib.sha256(raw).hexdigest() == PurePosixPath(ref).stem
    return raw


def saved_features(model):
    refs = json.loads(verified_bytes(model.metadata_["feature_manifest"]))
    assert len(refs) == 8  # Seven-day bounded batches, with a final one-day batch.
    return pd.concat([pd.read_parquet(io.BytesIO(verified_bytes(ref))) for ref in refs], ignore_index=True)


def counts(case, start, end):
    result = dict.fromkeys(case.grid.ids, 0)
    for event in case.records:
        if start <= event["origin_time"] < end and event["magnitude"] >= 4:
            result[case.grid.assign(event["longitude"], event["latitude"])] += 1
    return result


def assert_saved_rows(case, model):
    frame = saved_features(model)
    partitions = model.metadata_["partitions"]
    bounds = {name: tuple(map(utc, partitions[name])) for name in ("fit", "tune", "calibration")}
    assert len(frame) == 600 and not frame.duplicated(["day", "cell_id"]).any()
    positives = dict.fromkeys(bounds, 0)
    cell_days = dict.fromkeys(bounds, 0)
    for day, rows in frame.groupby("day", sort=True):
        start = utc(pd.Timestamp(day * 86400, unit="s", tz="UTC").to_pydatetime())
        end = start + timedelta(days=1)
        memberships = [name for name, (left, right) in bounds.items() if left <= start and end <= right]
        assert len(memberships) == 1
        (partition,) = memberships
        assert set(rows["cell_id"]) == set(case.grid.ids)
        observed = counts(case, start, end)
        feature_cutoff = start - timedelta(minutes=30)
        for row in rows.itertuples(index=False):
            assert row.y == int(observed[row.cell_id] > 0)
            positives[partition] += row.y
            cell_days[partition] += 1
            # Raw UTC timestamps and cell assignments form an independent oracle
            # for every count feature, including both half-open boundaries.
            for days in (1, 7, 30, 90):
                eligible = [
                    event
                    for event in case.records
                    if feature_cutoff - timedelta(days=days) <= event["origin_time"] < feature_cutoff
                    and case.grid.assign(event["longitude"], event["latitude"]) == row.cell_id
                ]
                assert getattr(row, f"count_{days}d") == len(eligible)
                if days in (7, 30):
                    assert getattr(row, f"target_{days}d") == sum(
                        event["magnitude"] >= 4 for event in eligible
                    )
    assert cell_days == {"fit": 360, "tune": 120, "calibration": 120}
    assert model.metadata_["cell_day_counts"] == cell_days
    assert model.metadata_["positive_counts"] == positives
    with transaction() as session:
        saved = list(
            session.scalars(select(FeatureSet).where(FeatureSet.experiment_id == case.experiment.id))
        )
        assert any(
            row.artifact == model.metadata_["feature_manifest"] and row.partitions == partitions
            for row in saved
        )
        snapshot = session.get(Snapshot, model.snapshot_id)
        revisions = list(session.scalars(select(Revision).where(Revision.id.in_(snapshot.revision_ids))))
    assert revisions and all(row.origin_time < model.training_cutoff for row in revisions)
    assert not any(row.origin_time == model.training_cutoff for row in revisions)
    first = frame[frame["day"] == bounds["fit"][0].timestamp() / 86400].set_index("cell_id")
    if bounds["fit"][0] == FIT_START:
        assert first.loc[case.grid.ids[10], "count_1d"] == 1
        assert first.loc[case.grid.ids[11], "count_1d"] == 0
    return frame


def assert_classifier_equal(left, right):
    assert isinstance(left, HistGradientBoostingClassifier) and isinstance(
        right, HistGradientBoostingClassifier
    )
    assert left.get_params() == right.get_params()
    assert left.n_iter_ == right.n_iter_ == 60
    np.testing.assert_array_equal(left.classes_, right.classes_)
    np.testing.assert_array_equal(left._baseline_prediction, right._baseline_prediction)
    for left_stage, right_stage in zip(left._predictors, right._predictors, strict=True):
        for left_tree, right_tree in zip(left_stage, right_stage, strict=True):
            np.testing.assert_array_equal(left_tree.nodes, right_tree.nodes)
            np.testing.assert_array_equal(left_tree.raw_left_cat_bitsets, right_tree.raw_left_cat_bitsets)
            np.testing.assert_array_equal(
                left_tree.binned_left_cat_bitsets, right_tree.binned_left_cat_bitsets
            )


def assert_calibrator_equal(left, right):
    assert isinstance(left, LogisticRegression) and isinstance(right, LogisticRegression)
    assert left.get_params() == right.get_params()
    np.testing.assert_array_equal(left.coef_, right.coef_)
    np.testing.assert_array_equal(left.intercept_, right.intercept_)
    np.testing.assert_array_equal(left.classes_, right.classes_)


def prediction_from_saved_components(case, model, start=START):
    verified_bytes(model.artifact)
    artifact = archive.load_model(model)
    if isinstance(artifact, HistoricalRate):
        return -np.expm1(-artifact.rates)
    events = {
        "t": np.array([row["origin_time"].timestamp() / 86400 for row in case.records]),
        "cell": np.array(
            [case.grid.lookup[case.grid.assign(row["longitude"], row["latitude"])] for row in case.records]
        ),
        "m": np.array([row["magnitude"] for row in case.records]),
        "depth": np.array([row["depth_km"] for row in case.records]),
    }
    cutoff = start - timedelta(minutes=30)
    x = features(events, case.grid, cutoff.timestamp() / 86400, 4, artifact["history_start"])
    assert x[10, NAMES.index("count_1d")] == 1
    assert x[11, NAMES.index("count_1d")] == 0
    raw = artifact["classifier"].predict_proba(x)[:, 1]
    assert np.ptp(raw) > 0  # The real fitted classifier is not a constant dummy.
    if artifact["kind"] == "hybrid":
        weight = artifact["weight_ml"]
        raw = weight * raw + (1 - weight) * (-np.expm1(-artifact["rate"].rates))
    log_odds = np.log(np.clip(raw, 1e-8, 1 - 1e-8) / (1 - np.clip(raw, 1e-8, 1 - 1e-8)))
    calibration = artifact["calibration"]
    return 1 / (1 + np.exp(-(log_odds * calibration.coef_[0, 0] + calibration.intercept_[0])))


def test_held_out_outcomes_do_not_change_real_fits_or_first_saved_forecast():
    left, right = make_case(), make_case(changed_test=True)
    assert (
        left.experiment.id != right.experiment.id and left.experiment.source_id != right.experiment.source_id
    )
    left_models, right_models = fit(left), fit(right)
    assert {model.id for model in left_models.values()}.isdisjoint(
        model.id for model in right_models.values()
    )
    for models in (left_models, right_models):
        assert all(models[name].status == "available" for name in ALGORITHMS)
        etas = models["etas_discrete_space"]
        assert etas.status == "insufficient_data" and etas.artifact is None
        assert etas.metadata_["training_windows"]["fit"]["event_count"] < 40
    left_frame = assert_saved_rows(left, left_models["hist_gradient_boosting"])
    right_frame = assert_saved_rows(right, right_models["hist_gradient_boosting"])
    pd.testing.assert_frame_equal(left_frame, right_frame, check_exact=True)
    assert (
        left_models["hist_gradient_boosting"].metadata_["feature_manifest"]
        == right_models["hist_gradient_boosting"].metadata_["feature_manifest"]
    )
    for algorithm in ALGORITHMS:
        a, b = left_models[algorithm], right_models[algorithm]
        assert a.metadata_["training_signature"] != b.metadata_["training_signature"]
        assert a.metadata_["partitions"] == b.metadata_["partitions"]
        assert a.metadata_["training_windows"] == b.metadata_["training_windows"]
        loaded_a, loaded_b = archive.load_model(a), archive.load_model(b)
        if algorithm == "historical_rate":
            assert loaded_a.sigma_cells == loaded_b.sigma_cells
            assert loaded_a.shrinkage == loaded_b.shrinkage
            assert loaded_a.training_days == loaded_b.training_days == 30
            np.testing.assert_array_equal(loaded_a.rates, loaded_b.rates)
            assert a.metadata_["tuning_brier"] == b.metadata_["tuning_brier"]
        else:
            assert_classifier_equal(loaded_a["classifier"], loaded_b["classifier"])
            assert_calibrator_equal(loaded_a["calibration"], loaded_b["calibration"])
            if algorithm == "calibrated_hybrid":
                assert loaded_a["weight_ml"] == loaded_b["weight_ml"]
                np.testing.assert_array_equal(loaded_a["rate"].rates, loaded_b["rate"].rates)
                assert a.metadata_["components"] == [left_models[name].id for name in ALGORITHMS[:2]]
                assert b.metadata_["components"] == [right_models[name].id for name in ALGORITHMS[:2]]
        expected_left = prediction_from_saved_components(left, a)
        expected_right = prediction_from_saved_components(right, b)
        np.testing.assert_array_equal(expected_left, expected_right)
        np.testing.assert_array_equal(expected_left, prediction_from_saved_components(left, a))
        for case, model, expected in ((left, a, expected_left), (right, b, expected_right)):
            forecast = archive.issue_forecast(
                case.experiment.id, model.id, START, mode="retrospective_revised"
            )
            with transaction() as session:
                cells = {
                    row.cell_id: row.probability
                    for row in session.scalars(
                        select(ForecastCell).where(ForecastCell.forecast_id == forecast.id)
                    )
                }
            np.testing.assert_allclose(
                [cells[cell] for cell in case.grid.ids], expected, rtol=1e-13, atol=1e-15
            )
            evaluation = archive.evaluate_forecast(forecast.id)
            assert evaluation.status == "provisional" and evaluation.metrics["n"] == 12
            with transaction() as session:
                observed = {
                    row.cell_id: row.observed_count
                    for row in session.scalars(
                        select(EvaluationCell).where(EvaluationCell.evaluation_id == evaluation.id)
                    )
                }
            assert observed == counts(case, START, START + timedelta(days=1))
    assert counts(left, START, START + timedelta(days=1)) != counts(right, START, START + timedelta(days=1))


def test_two_real_backtest_folds_archive_only_earlier_complete_training_windows():
    case = make_case()
    progress = []
    result = tasks.run_backtest(
        case.experiment.id,
        START,
        test_days=2,
        folds=2,
        fit_days=30,
        tune_days=10,
        calibration_days=10,
        progress=progress.append,
    )
    assert result["mode"] == "retrospective_revised"
    assert len(result["models"]) == len(set(result["models"])) == 8
    assert len(result["forecasts"]) == 12
    assert progress[-1] == 1 and all(left <= right for left, right in pairwise(progress))
    with transaction() as session:
        models = {row.id: row for row in session.scalars(select(Model).where(Model.id.in_(result["models"])))}
        forecasts = {
            row.id: row
            for row in session.scalars(select(Forecast).where(Forecast.experiment_id == case.experiment.id))
        }
        evaluations = {
            row.id: row
            for row in session.scalars(
                select(Evaluation).where(
                    Evaluation.id.in_([row["evaluation"] for row in result["forecasts"]])
                )
            )
        }
    for fold in range(2):
        target_start = START + timedelta(days=2 * fold)
        expected_cutoff = target_start - timedelta(days=1)
        fold_models = [models[identity] for identity in result["models"][fold * 4 : (fold + 1) * 4]]
        available = [model for model in fold_models if model.status == "available"]
        assert {model.algorithm for model in available} == set(ALGORITHMS)
        for model in fold_models:
            assert model.training_cutoff == expected_cutoff
            bounds = model.metadata_["partitions"]
            assert utc(bounds["fit"][1]) == utc(bounds["tune"][0])
            assert utc(bounds["tune"][1]) == utc(bounds["calibration"][0])
            assert utc(bounds["calibration"][1]) == expected_cutoff < target_start - timedelta(minutes=30)
        ml = next(model for model in available if model.algorithm == "hist_gradient_boosting")
        frame = assert_saved_rows(case, ml)
        assert frame["day"].max() * 86400 + 86400 == expected_cutoff.timestamp()
        if fold:
            assert frame["day"].max() == START.timestamp() / 86400
            # The already completed first test day may legitimately train fold2.
            observed = counts(case, START, START + timedelta(days=1))
            last = frame[frame["day"] == frame["day"].max()].set_index("cell_id")
            assert {cell: int(last.loc[cell, "y"]) for cell in case.grid.ids} == {
                cell: int(value > 0) for cell, value in observed.items()
            }
        records = result["forecasts"][fold * 6 : (fold + 1) * 6]
        assert [forecasts[row["forecast"]].start for row in records] == [target_start] * 3 + [
            target_start + timedelta(days=1)
        ] * 3
        for row in records:
            forecast = forecasts[row["forecast"]]
            evaluation = evaluations[row["evaluation"]]
            assert forecast.model_id in {model.id for model in available}
            assert forecast.end == forecast.start + timedelta(days=1)
            assert forecast.input_cutoff == forecast.start - timedelta(minutes=30)
            assert forecast.provenance["model_artifact"] == models[forecast.model_id].artifact
            assert evaluation.forecast_id == forecast.id and evaluation.status == "provisional"
            assert evaluation.metrics["n"] == 12
    assert max(models[identity].created_at for identity in result["models"][:4]) <= min(
        models[identity].created_at for identity in result["models"][4:]
    )


def test_actual_insufficient_calibration_keeps_rate_but_no_ml_or_hybrid_artifacts():
    case = make_case(sparse_calibration=True)
    models = fit(case)
    assert models["historical_rate"].status == "available"
    frame = assert_saved_rows(case, models["historical_rate"])
    calibration = frame[frame["day"] >= CAL_START.timestamp() / 86400]
    assert 0 < calibration["y"].sum() < 20 and set(calibration["y"]) == {0, 1}
    for algorithm in ("hist_gradient_boosting", "calibrated_hybrid"):
        model = models[algorithm]
        assert model.status == "insufficient_data" and model.artifact is None
        assert model.metadata_["training_windows_status"] == "planned_unavailable"
    assert "At least 20 positives" in models["hist_gradient_boosting"].metadata_["reason"]
