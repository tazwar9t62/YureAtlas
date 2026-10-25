"""Saved fitting windows match consumed data; optimizer spies test plumbing only.

Rate fitting and persisted feature batches are real. Classifier/calibrator spies
and the ETAS optimizer stand-in deliberately do not establish model quality or
ETAS numerical correctness; those have separate chronological/scientific tests.
"""

import csv
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select
from yureatlas import archive, etas, learning
from yureatlas.artifacts import canonical, digest, put, read
from yureatlas.catalogue import ensure_experiment, ingest_records, load_experiment, make_snapshot
from yureatlas.db import transaction
from yureatlas.features import NAMES
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import FeatureSet, Model, Snapshot
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration
CUTOFF = utc("2024-07-15T15:00:00Z")


class RecordingClassifier:
    """A deterministic probe of which matrices the orchestration consumes."""

    instances: ClassVar[list] = []

    def __init__(self, **parameters):
        self.parameters = parameters
        self.predictions = []
        type(self).instances.append(self)

    def fit(self, x, y):
        self.fit_x, self.fit_y = x.copy(), y.copy()
        return self

    def predict_proba(self, x):
        probability = np.where(x[:, 0] > 0, 0.8, 0.1)
        self.predictions.append((x.copy(), probability.copy()))
        return np.column_stack((1 - probability, probability))

    def get_params(self):
        return self.parameters


class RecordingCalibration:
    instances: ClassVar[list] = []

    def __init__(self, **parameters):
        self.parameters = parameters
        type(self).instances.append(self)

    def fit(self, x, y):
        self.fit_x, self.fit_y = x.copy(), y.copy()
        return self


@pytest.fixture
def fitting_probes(monkeypatch):
    RecordingClassifier.instances = []
    RecordingCalibration.instances = []
    optimizer_calls = []

    def no_optimization(objective, initial, **options):
        optimizer_calls.append(options)
        theta = np.asarray(initial, dtype=float)
        return SimpleNamespace(
            success=True,
            x=theta,
            fun=objective(theta),
            nit=0,
            message="Window-plumbing fixture: no optimization performed",
        )

    monkeypatch.setattr(learning, "HistGradientBoostingClassifier", RecordingClassifier)
    monkeypatch.setattr(learning, "LogisticRegression", RecordingCalibration)
    monkeypatch.setattr(etas, "minimize", no_optimization)
    return optimizer_calls


def record(identity, origin, *, coordinates=(140.5, 35.5), magnitude=4.2):
    return {
        "provider_id": identity,
        "origin_time": origin,
        "provider_updated": CUTOFF + timedelta(days=1),
        "longitude": float(coordinates[0]),
        "latitude": float(coordinates[1]),
        "depth_km": 12,
        "magnitude": magnitude,
        "magnitude_type": "J",
        "status": "reviewed",
        "event_type": "earthquake",
    }


def catalogue_case(fit_days=30, *, records=None, coverage_start=None):
    identity = "windows_" + uuid4().hex[:12]
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
    case = SimpleNamespace(experiment=experiment, grid=grid, fit_days=fit_days, tune_days=10, cal_days=10)
    case.cutoff = CUTOFF
    case.cal_start = CUTOFF - timedelta(days=10)
    case.tune_start = case.cal_start - timedelta(days=10)
    case.fit_start = case.tune_start - timedelta(days=fit_days)
    case.first_feature_cutoff = case.fit_start - timedelta(minutes=30)
    case.warm_start = case.first_feature_cutoff - timedelta(days=90)
    case.etas_start = max(case.fit_start, case.tune_start - timedelta(days=90))
    if records is None:
        records = []
        for day in range(fit_days + 20):
            start = case.fit_start + timedelta(days=day)
            # Calibration labels differ from tuning labels, so exchanging the
            # partitions cannot accidentally satisfy the matrix assertions.
            active = (0, 2) if start >= case.cal_start else (0, 1)
            for cell in active:
                records.append(
                    record(
                        f"day-{day}-cell-{cell}", start + timedelta(hours=12), coordinates=grid.lonlat[cell]
                    )
                )
        sentinels = {
            "before_context": case.warm_start - timedelta(seconds=1),
            "context_start": case.warm_start,
            "formerly_missing_warmup": case.warm_start + timedelta(minutes=15),
            "before_first_cutoff": case.first_feature_cutoff - timedelta(seconds=1),
            "exact_first_cutoff": case.first_feature_cutoff,
            "before_fit": case.fit_start - timedelta(seconds=1),
            "fit_start": case.fit_start,
            "before_etas": case.etas_start - timedelta(seconds=1),
            "etas_start": case.etas_start,
            "before_tuning": case.tune_start - timedelta(seconds=1),
            "tuning_start": case.tune_start,
            "before_calibration": case.cal_start - timedelta(seconds=1),
            "calibration_start": case.cal_start,
            "before_end": case.cutoff - timedelta(seconds=1),
            "exact_end": case.cutoff,
        }
        for name, origin in sentinels.items():
            cell = 4 if name == "before_context" else 3
            records.append(record(name, origin, coordinates=grid.lonlat[cell], magnitude=4.8))
        records.append(
            record(
                "input_only", case.fit_start + timedelta(hours=6), coordinates=grid.lonlat[2], magnitude=3.2
            )
        )
    case.records = records
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(record("", CUTOFF)))
    writer.writeheader()
    writer.writerows(records)
    raw = buffer.getvalue().encode()
    artifact = put(raw, "csv")
    ingest_records(
        experiment.source_id,
        records,
        coverage_start if coverage_start is not None else case.warm_start,
        CUTOFF + timedelta(days=1),
        {"raw_ref": artifact},
        received_at=CUTOFF + timedelta(days=2),
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
    return case


def train(case):
    return {
        model.algorithm: model
        for model in learning.train_suite(
            case.experiment.id,
            case.cutoff,
            fit_days=case.fit_days,
            tune_days=case.tune_days,
            calibration_days=case.cal_days,
        )
    }


def expected_count(case, start, end, *, target=True):
    return sum(
        start <= row["origin_time"] < end and (not target or row["magnitude"] >= 4) for row in case.records
    )


def expected_labels(case, start, days):
    result = []
    for day in range(days):
        begin = start + timedelta(days=day)
        affected = {
            case.grid.assign(row["longitude"], row["latitude"])
            for row in case.records
            if begin <= row["origin_time"] < begin + timedelta(days=1) and row["magnitude"] >= 4
        }
        result.extend(int(cell in affected) for cell in case.grid.ids)
    return np.asarray(result)


def assert_window(window, start, end, role):
    assert utc(window["start"]) == start
    assert utc(window["end"]) == end
    assert window["duration_days"] == pytest.approx((end - start).total_seconds() / 86400)
    assert window["role"] == role


def feature_frame(model):
    refs = json.loads(read(model.metadata_["feature_manifest"]))
    return pd.concat([pd.read_parquet(io.BytesIO(read(ref))) for ref in refs], ignore_index=True)


@pytest.mark.parametrize("fit_days", [30, 120])
def test_rate_and_etas_consume_only_their_saved_fit_windows(fitting_probes, fit_days):
    case = catalogue_case(fit_days)
    models = train(case)
    assert all(model.status == "available" for model in models.values())
    assert len(fitting_probes) == 1  # Actual ETAS filtering; optimizer is explicitly a stand-in.
    rate = models["historical_rate"]
    rate_artifact = archive.load_model(rate)
    expected = expected_count(case, case.fit_start, case.tune_start)
    assert rate_artifact.training_days == fit_days
    assert rate_artifact.rates.sum() == pytest.approx(expected / fit_days)
    assert_window(
        rate.metadata_["training_windows"]["fit"], case.fit_start, case.tune_start, "target_event_rate_fit"
    )
    assert rate.metadata_["training_windows"]["fit"]["event_count"] == expected
    assert rate.metadata_["training_windows"]["context"] is None
    etas_model = models["etas_discrete_space"]
    etas_artifact = archive.load_model(etas_model)
    etas_count = expected_count(case, case.etas_start, case.tune_start)
    assert etas_artifact.diagnostics["fit_events"] == etas_count
    assert etas_artifact.diagnostics["fit_days"] == min(fit_days, 90)
    assert_window(
        etas_model.metadata_["training_windows"]["fit"],
        case.etas_start,
        case.tune_start,
        "conditional_point_process_fit_empty_initial_history",
    )
    assert etas_model.metadata_["training_windows"]["fit"]["event_count"] == etas_count
    assert all(
        etas_model.metadata_["training_windows"][name] is None
        for name in ("context", "selection", "calibration", "diagnostic")
    )
    for model in models.values():
        assert model.metadata_["training_recipe"] == "chronological-windows-v2"
        assert model.metadata_["training_windows_status"] == "completed"
        assert model.metadata_["partitions"]["fit"] == [
            case.fit_start.isoformat(),
            case.tune_start.isoformat(),
        ]


def test_saved_feature_context_and_supervised_roles_match_consumed_matrices(fitting_probes):
    case = catalogue_case()
    models = train(case)
    rate = models["historical_rate"]
    ml = models["hist_gradient_boosting"]
    hybrid = models["calibrated_hybrid"]
    frame = feature_frame(ml)
    first = frame[frame["day"] == case.fit_start.timestamp() / 86400].set_index("cell_id")
    assert first.loc[case.grid.ids[3], "count_90d"] == 3
    assert first.loc[case.grid.ids[3], "count_1d"] == 1
    assert first.loc[case.grid.ids[4], "count_90d"] == 0
    context = ml.metadata_["training_windows"]["context"]
    last_cutoff = case.cutoff - timedelta(days=1, minutes=30)
    assert_window(context, case.warm_start, last_cutoff, "per_row_feature_inputs")
    assert utc(context["warmup_end"]) == utc(context["first_row_cutoff"]) == case.first_feature_cutoff
    assert context["event_count"] == expected_count(case, case.warm_start, last_cutoff, target=False)
    assert context["per_row_cutoff_minutes"] == 30
    assert hybrid.metadata_["training_windows"]["context"] == context
    (classifier,) = RecordingClassifier.instances
    raw_calibration, hybrid_calibration = RecordingCalibration.instances
    fit_n = case.fit_days * len(case.grid.ids)
    tune_n = case.tune_days * len(case.grid.ids)
    x = frame[NAMES].to_numpy()
    fit_y = expected_labels(case, case.fit_start, case.fit_days)
    tune_y = expected_labels(case, case.tune_start, case.tune_days)
    cal_y = expected_labels(case, case.cal_start, case.cal_days)
    assert not np.array_equal(tune_y, cal_y)
    np.testing.assert_array_equal(frame["y"].to_numpy(), np.concatenate((fit_y, tune_y, cal_y)))
    np.testing.assert_allclose(classifier.fit_x, x[:fit_n], equal_nan=True)
    np.testing.assert_array_equal(classifier.fit_y, fit_y)
    np.testing.assert_allclose(classifier.predictions[0][0], x[fit_n : fit_n + tune_n], equal_nan=True)
    np.testing.assert_allclose(classifier.predictions[1][0], x[fit_n + tune_n :], equal_nan=True)
    assert classifier.parameters["early_stopping"] is False
    assert ml.metadata_["training_windows"]["selection"] is None
    assert_window(
        ml.metadata_["training_windows"]["diagnostic"],
        case.tune_start,
        case.cal_start,
        "raw_classifier_brier_diagnostic_only",
    )
    assert_window(
        rate.metadata_["training_windows"]["selection"],
        case.tune_start,
        case.cal_start,
        "rate_hyperparameter_selection",
    )
    assert_window(
        hybrid.metadata_["training_windows"]["selection"],
        case.tune_start,
        case.cal_start,
        "mixture_weight_and_rate_hyperparameter_selection",
    )
    assert_window(
        ml.metadata_["training_windows"]["calibration"],
        case.cal_start,
        case.cutoff,
        "sigmoid_fit_on_raw_classifier",
    )
    assert_window(
        hybrid.metadata_["training_windows"]["calibration"],
        case.cal_start,
        case.cutoff,
        "sigmoid_fit_on_selected_mixture",
    )
    for model in (ml, hybrid):
        windows = model.metadata_["training_windows"]
        for name, labels in (("fit", fit_y), ("calibration", cal_y)):
            assert windows[name]["cell_day_count"] == len(labels)
            assert windows[name]["positive_cell_day_count"] == int(labels.sum())
    raw_tune, raw_cal = (prediction[1] for prediction in classifier.predictions)
    assert ml.metadata_["tuning_brier"] == pytest.approx(float(np.mean((raw_tune - tune_y) ** 2)))
    baseline = archive.load_model(rate).predict()
    losses = {
        step / 20: float(
            np.mean(
                ((1 - step / 20) * np.tile(baseline, case.tune_days) + step / 20 * raw_tune - tune_y) ** 2
            )
        )
        for step in range(21)
    }
    chosen = min(losses, key=losses.get)
    assert hybrid.metadata_["weights"]["ml"] == pytest.approx(chosen)
    np.testing.assert_allclose(raw_calibration.fit_x[:, 0], np.log(raw_cal / (1 - raw_cal)))
    mixed_cal = (1 - chosen) * np.tile(baseline, case.cal_days) + chosen * raw_cal
    np.testing.assert_allclose(hybrid_calibration.fit_x[:, 0], np.log(mixed_cal / (1 - mixed_cal)))
    np.testing.assert_array_equal(raw_calibration.fit_y, cal_y)
    np.testing.assert_array_equal(hybrid_calibration.fit_y, cal_y)


def test_coverage_only_changes_invalidate_availability_without_rewriting_archives(
    fitting_probes, monkeypatch
):
    fit_start = CUTOFF - timedelta(days=50)
    case = catalogue_case(coverage_start=fit_start - timedelta(days=90))
    receipt_clock = [CUTOFF + timedelta(days=2, hours=1)]
    monkeypatch.setattr(learning, "now", lambda: receipt_clock[0])
    models = train(case)
    assert not fitting_probes and not RecordingClassifier.instances
    assert all(model.status == "insufficient_data" and model.artifact is None for model in models.values())
    assert all(
        model.metadata_["training_windows_status"] == "planned_unavailable" for model in models.values()
    )
    context = models["hist_gradient_boosting"].metadata_["training_windows"]["context"]
    assert utc(context["start"]) == fit_start - timedelta(days=90, minutes=30)
    missing_ids = {model.id for model in models.values()}

    def saved_rows(ids):
        with transaction() as session:
            return canonical(
                [
                    dict(row)
                    for row in session.execute(
                        select(Model.__table__).where(Model.id.in_(ids)).order_by(Model.id)
                    ).mappings()
                ]
            )

    def revision_ids(model):
        with transaction() as session:
            return session.get(Snapshot, model.snapshot_id).revision_ids

    original_rows = saved_rows(missing_ids)
    original_revisions = revision_ids(models["historical_rate"])
    initial_cutoff = receipt_clock[0]
    coverage = models["historical_rate"].metadata_["training_coverage"]
    assert coverage == {
        "start": case.warm_start.isoformat(),
        "end": case.cutoff.isoformat(),
        "complete": False,
        "policy": "complete_retrieval_asof_training_snapshot",
        "receipt_cutoff": initial_cutoff.isoformat(),
    }

    def coverage_receipt(namespace, receipt, status="complete"):
        # An empty, verified CSV changes retrieval evidence only. It appends no
        # event revisions, so the former revision-only cache cannot notice it.
        raw = (",".join(record("", CUTOFF)) + "\n").encode()
        artifact = put(raw, "csv")
        ingest_records(
            case.experiment.source_id,
            [],
            case.warm_start,
            case.cutoff,
            {"raw_ref": artifact},
            received_at=receipt,
            status=status,
            import_identity={
                "catalogue_namespace": case.experiment.config["catalogue_namespace"],
                "namespace": namespace,
                "format": "csv",
                "artifact": artifact,
                "line_count": 1,
                "mapping": {},
                "parser_version": "csv-v2",
                "coverage_declared": True,
            },
        )

    repaired_at = initial_cutoff + timedelta(hours=1)
    coverage_receipt("coverage-repair", repaired_at)
    assert {model.id for model in train(case).values()} == missing_ids
    assert not fitting_probes  # Repair received after this snapshot is invisible.
    receipt_clock[0] = repaired_at
    repaired = train(case)
    repaired_ids = {model.id for model in repaired.values()}
    assert repaired_ids.isdisjoint(missing_ids)
    assert all(model.status == "available" for model in repaired.values())
    assert all(model.metadata_["training_coverage"]["complete"] for model in repaired.values())
    assert revision_ids(repaired["historical_rate"]) == original_revisions
    assert saved_rows(missing_ids) == original_rows
    archived_rows = saved_rows(missing_ids | repaired_ids)
    artifact_bytes = {model.artifact: read(model.artifact) for model in repaired.values()}

    receipt_clock[0] += timedelta(minutes=1)
    assert {model.id for model in train(case).values()} == repaired_ids
    partial_at = receipt_clock[0] + timedelta(hours=1)
    coverage_receipt("coverage-partial", partial_at, "partial")
    assert {model.id for model in train(case).values()} == repaired_ids
    receipt_clock[0] = partial_at
    blocked = train(case)
    assert all(model.status == "insufficient_data" for model in blocked.values())
    assert repaired_ids.isdisjoint(model.id for model in blocked.values())
    assert revision_ids(blocked["historical_rate"]) == original_revisions

    receipt_clock[0] += timedelta(hours=1)
    coverage_receipt("coverage-restored", receipt_clock[0])
    restored = train(case)
    assert {model.id for model in restored.values()} == repaired_ids
    assert all(
        utc(model.metadata_["training_coverage"]["receipt_cutoff"]) == repaired_at
        for model in restored.values()
    )  # Equivalent eligible retries retain the original model/snapshot evidence.
    assert len(fitting_probes) == len(RecordingClassifier.instances) == 1
    assert saved_rows(missing_ids | repaired_ids) == archived_rows
    assert {ref: read(ref) for ref in artifact_bytes} == artifact_bytes
    with transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(FeatureSet)
                .where(FeatureSet.experiment_id == case.experiment.id)
            )
            == 1
        )


def test_new_recipe_does_not_reuse_or_rewrite_legacy_model_archives(fitting_probes):
    case = catalogue_case()
    snapshot, _ = make_snapshot(
        case.experiment.source_id, archive.now(), "retrospective_revised", origin_before=case.cutoff
    )
    # Frozen v1 signature recipe: changing model windows must not silently reuse
    # archived models simply because their revision set and outer partitions match.
    partitions = {
        "fit": [case.fit_start.isoformat(), case.tune_start.isoformat()],
        "tune": [case.tune_start.isoformat(), case.cal_start.isoformat()],
        "calibration": [case.cal_start.isoformat(), case.cutoff.isoformat()],
        "untouched_test": "strictly after training cutoff; never consumed here",
        "availability": "retrospective_revised",
        "grouping": "all cells on a date stay together",
    }
    legacy_signature = digest(
        {
            "experiment": case.experiment.config_hash,
            "partitions": partitions,
            "revisions": snapshot.revision_ids,
            "schema": "cell-day-v1",
            "recipe": "v1",
        }
    )
    legacy = [
        archive.save_model(
            case.experiment,
            algorithm,
            {"legacy_fixture": algorithm},
            case.cutoff,
            snapshot.id,
            {"training_signature": legacy_signature, "partitions": partitions},
        )
        for algorithm in (
            "historical_rate",
            "hist_gradient_boosting",
            "calibrated_hybrid",
            "etas_discrete_space",
        )
    ]
    ids = {model.id for model in legacy}

    def old_rows():
        with transaction() as session:
            return canonical(
                [
                    dict(row)
                    for row in session.execute(
                        select(Model.__table__).where(Model.id.in_(ids)).order_by(Model.id)
                    ).mappings()
                ]
            )

    original_rows = old_rows()
    original_bytes = {model.artifact: read(model.artifact) for model in legacy}
    current = train(case)
    assert ids.isdisjoint(model.id for model in current.values())
    assert all(model.metadata_["training_signature"] != legacy_signature for model in current.values())
    assert old_rows() == original_rows
    assert {ref: read(ref) for ref in original_bytes} == original_bytes
    with transaction() as session:
        feature_count = session.scalar(
            select(func.count()).select_from(FeatureSet).where(FeatureSet.experiment_id == case.experiment.id)
        )
    repeated = train(case)
    assert {model.id for model in repeated.values()} == {model.id for model in current.values()}
    assert len(fitting_probes) == len(RecordingClassifier.instances) == 1
    with transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(FeatureSet)
                .where(FeatureSet.experiment_id == case.experiment.id)
            )
            == feature_count
        )
    assert old_rows() == original_rows


@pytest.mark.parametrize("target_count", [4, 5])
def test_standalone_rate_records_exact_inferred_history_and_target_event_count(target_count):
    first = utc("2024-06-01T00:00:00.123457Z")
    records = [record(f"target-{index}", first + timedelta(hours=index)) for index in range(target_count)]
    records.extend(
        record(f"input-{index}", first + timedelta(hours=6 + index), magnitude=3.2) for index in range(6)
    )
    case = catalogue_case(records=records)
    cutoff = first + timedelta(days=1)
    model = archive.train_rate(case.experiment.id, cutoff)
    windows = model.metadata_["training_windows"]
    assert_window(windows["fit"], first, cutoff, "target_event_rate_fit")
    assert windows["fit"]["event_count"] == target_count
    assert all(windows[name] is None for name in ("context", "selection", "calibration", "diagnostic"))
    assert model.metadata_["training_recipe"] == "fixed-rate-window-v2"
    if target_count == 5:
        assert model.status == "available"
        assert model.metadata_["training_windows_status"] == "completed"
        artifact = archive.load_model(model)
        assert artifact.training_days == pytest.approx(1)
        assert artifact.rates.sum() == pytest.approx(5)
    else:
        assert model.status == "insufficient_data" and model.artifact is None
        assert model.metadata_["training_windows_status"] == "planned_unavailable"
        assert "received 4" in model.metadata_["reason"]
