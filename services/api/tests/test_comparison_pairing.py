"""Reference choice follows archived provenance and compatible observations."""

from copy import deepcopy
from datetime import timedelta
from itertools import permutations
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from yureatlas import api
from yureatlas.artifacts import digest
from yureatlas.catalogue import ensure_experiment, load_experiment
from yureatlas.comparisons import build_comparison_report
from yureatlas.db import transaction
from yureatlas.metrics import score
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Evaluation, EvaluationCell, Forecast, ForecastCell, Model, Snapshot, Source
from yureatlas.timeutil import utc

START = utc("2021-07-01T15:00:00Z")
END = START + timedelta(days=1)
PUBLISHED = START - timedelta(hours=1)


def record(identity, *, algorithm="candidate", **changes):
    """Two observed cells and one missing cell, independent of any DB ordering."""
    return {
        "experiment_id": "experiment",
        "start": START,
        "end": END,
        "mode": "synthetic_demo",
        "grid_hash": "grid",
        "grid_cell_ids": ["cell-a", "cell-b", "cell-c"],
        "forecast_cell_ids": ["cell-a", "cell-b", "cell-c"],
        "forecast_id": identity,
        "forecast_created_at": PUBLISHED,
        "model_id": "model-" + identity,
        "algorithm": algorithm,
        "evaluation_id": "evaluation-" + identity,
        "evaluation_version": 1,
        "status": "provisional",
        "revision_ids": ["revision-a", "revision-b"],
        "observations": [("cell-a", 0), ("cell-b", 1)],
        "metrics": score([0.2, 0.3], [0, 1], missing=1),
        **changes,
    }


def reference(identity, **changes):
    return record(identity, algorithm="historical_rate", **changes)


def decision(report, forecast_id):
    return next(row for row in report["decisions"] if row["forecast_id"] == forecast_id)


def comparison(report, model_id):
    return next(row for row in report["comparisons"] if row["model_id"] == model_id)


def test_earliest_compatible_archive_wins_independently_of_order_and_score():
    candidate = record("candidate")
    incompatible = reference(
        "incompatible", forecast_created_at=PUBLISHED - timedelta(hours=2), revision_ids=["old-revision"]
    )
    chosen = reference("chosen", metrics=score([0.9, 0.1], [0, 1], missing=1))
    better_later = reference(
        "better-later",
        forecast_created_at=PUBLISHED + timedelta(seconds=1),
        metrics=score([0, 1], [0, 1], missing=1),
    )
    records = [candidate, incompatible, chosen, better_later]
    original = deepcopy(records)
    expected = build_comparison_report(records)
    for ordering in permutations(records):
        assert build_comparison_report(list(ordering)) == expected
    selected = decision(expected, "candidate")
    assert selected["status"] == "paired"
    assert selected["reference_forecast_id"] == "chosen"
    assert selected["reference_model_id"] == "model-chosen"
    assert selected["reference_evaluation_id"] == "evaluation-chosen"
    assert selected["reference_evaluation_version"] == 1
    assert selected["self_reference"] is False
    assert records == original


def test_equal_archive_times_use_model_then_forecast_identity():
    records = [
        record("candidate"),
        reference("forecast-a", model_id="model-z"),
        reference("forecast-c", model_id="model-a"),
        reference("forecast-b", model_id="model-a"),
    ]
    for ordering in permutations(records):
        assert decision(build_comparison_report(list(ordering)), "candidate")["reference_forecast_id"] == (
            "forecast-b"
        )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"start": START + timedelta(hours=1)}, "reference_not_found"),
        ({"end": END + timedelta(hours=1)}, "window_mismatch"),
        ({"experiment_id": "other-experiment"}, "experiment_mismatch"),
        ({"mode": "retrospective_revised"}, "mode_mismatch"),
        ({"grid_hash": "other-grid"}, "grid_mismatch"),
        ({"revision_ids": ["revision-a", "new-revision"]}, "revision_mismatch"),
        ({"observations": [("cell-a", 0), ("cell-c", 1)]}, "cell_mismatch"),
        ({"observations": [("cell-a", 1), ("cell-b", 0)]}, "label_mismatch"),
        ({"evaluation_id": None, "status": "not_evaluated", "metrics": None}, "reference_not_evaluated"),
        ({"status": "awaiting_data"}, "reference_observations_unavailable"),
        ({"status": "arbitrary"}, "reference_evidence_invalid"),
        ({"observations": [("cell-a", 0), ("cell-a", 1)]}, "reference_evidence_invalid"),
        ({"observations": [("cell-a", 0), ("outside-grid", 1)]}, "reference_evidence_invalid"),
        ({"forecast_cell_ids": ["cell-a", "cell-c"]}, "reference_evidence_invalid"),
    ],
)
def test_incompatible_reference_is_excluded_with_specific_evidence(changes, reason):
    report = build_comparison_report([record("candidate"), reference("reference", **changes)])
    selected = decision(report, "candidate")
    assert selected["status"] == "excluded"
    assert reason in selected["reasons"]
    assert selected["reference_forecast_id"] is None
    assert selected["reference_model_id"] is None
    assert selected["reference_evaluation_id"] is None
    row = comparison(report, "model-candidate")
    assert row["paired_days"] == 0 and row["excluded_days"] == 1
    assert row["exclusion_reasons"][reason] == 1


def test_revision_and_cell_order_do_not_change_the_observation_cohort():
    report = build_comparison_report(
        [
            record("candidate"),
            reference(
                "reference",
                revision_ids=["revision-b", "revision-a"],
                observations=[("cell-b", 1), ("cell-a", 0)],
            ),
        ]
    )
    candidate = decision(report, "candidate")
    baseline = decision(report, "reference")
    assert candidate["status"] == "paired"
    assert candidate["reference_forecast_id"] == "reference"
    for key in ("revision_signature", "cell_signature", "observation_signature"):
        assert candidate[key] == baseline[key] and candidate[key]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {
                "evaluation_id": None,
                "evaluation_version": None,
                "status": "not_evaluated",
                "revision_ids": None,
                "observations": None,
                "metrics": None,
            },
            "candidate_not_evaluated",
        ),
        (
            {
                "status": "awaiting_data",
                "metrics": score([], [], missing=3),
                "revision_ids": ["revision-b", "revision-a"],
            },
            "candidate_observations_unavailable",
        ),
        ({"observations": [("cell-a", 0)]}, "candidate_evidence_invalid"),
        ({"status": "arbitrary"}, "candidate_evidence_invalid"),
    ],
)
def test_unavailable_candidate_forecasts_remain_in_excluded_accounting(changes, reason):
    report = build_comparison_report([record("candidate", **changes), reference("reference")])
    selected = decision(report, "candidate")
    assert selected["status"] == "excluded" and selected["reasons"] == [reason]
    row = comparison(report, "model-candidate")
    assert row["total_forecasts"] == 1
    assert row["evaluated_forecasts"] == int(changes.get("evaluation_id", "exists") is not None)
    assert row["paired_days"] == 0 and row["excluded_days"] == 1
    assert row["exclusion_reasons"] == {reason: 1}
    evaluated = [row for row in report["series"] if row["forecast_id"] == "candidate"]
    if reason == "candidate_not_evaluated":
        assert evaluated == []
    elif reason == "candidate_observations_unavailable":
        signature = digest(["revision-a", "revision-b"])
        assert selected["revision_signature"] == signature
        assert evaluated[0]["snapshot"] == signature
        assert selected["cell_signature"] is None and selected["observation_signature"] is None


def test_absent_reference_is_explicit_and_a_baseline_can_reference_itself():
    missing = decision(build_comparison_report([record("candidate")]), "candidate")
    assert missing["status"] == "excluded" and missing["reasons"] == ["reference_not_found"]
    report = build_comparison_report([reference("reference")])
    selected = decision(report, "reference")
    assert selected["status"] == "paired" and selected["reasons"] == []
    assert selected["self_reference"] is True
    assert selected["reference_forecast_id"] == selected["forecast_id"]
    assert selected["reference_model_id"] == selected["model_id"]
    assert selected["reference_evaluation_id"] == selected["evaluation_id"]


def test_rolling_windows_keep_their_own_reference_provenance():
    next_window = {"start": END, "end": END + timedelta(days=1)}
    report = build_comparison_report(
        [
            record("first", model_id="candidate-model"),
            reference("first-baseline", model_id="first-fold"),
            record("second", model_id="candidate-model", **next_window),
            reference("second-baseline", model_id="second-fold", **next_window),
        ]
    )
    assert decision(report, "first")["reference_forecast_id"] == "first-baseline"
    assert decision(report, "second")["reference_forecast_id"] == "second-baseline"
    row = comparison(report, "candidate-model")
    assert row["reference_model_ids"] == ["first-fold", "second-fold"]
    assert row["reference_model_count"] == 2 and row["mixed_reference_models"] is True
    assert row["paired_days"] == 2 and row["excluded_days"] == 0


@pytest.fixture
def archived_comparison():
    """Use the ordinary publication triggers and immutable evaluation archive."""
    experiment = ensure_experiment(
        ExperimentSpec(
            name="comparison_" + uuid4().hex[:12],
            provider="synthetic",
            west=140,
            east=140.5,
            south=35,
            north=35.5,
        )
    )
    _, grid = load_experiment(experiment.id)

    def snapshot(session, *, foreign_source=False):
        source_id = experiment.source_id
        if foreign_source:
            source = Source(
                name="foreign_comparison_" + uuid4().hex,
                provider="synthetic",
                attribution="Separate catalogue acceptance fixture",
                details={},
            )
            session.add(source)
            session.flush()
            source_id = source.id
        row = Snapshot(
            id=str(uuid4()),
            source_id=source_id,
            digest=uuid4().hex + uuid4().hex,
            cutoff=END,
            policy="evaluation_revised",
            revision_ids=[],
        )
        session.add(row)
        session.flush()
        return row

    def append_evaluation(
        forecast, *, status="provisional", version=1, foreign_cell=False, foreign_source=False, labels=None
    ):
        identity = str(uuid4())
        counts = dict.fromkeys(grid.ids, 0) if labels is None else dict(labels)
        if foreign_cell:
            counts["outside-grid"] = counts.pop(grid.ids[0])
        with transaction() as session:
            observation = snapshot(session, foreign_source=foreign_source)
            metrics = (
                score([0.2] * len(counts), [int(value > 0) for value in counts.values()])
                if status == "provisional"
                else score([], [], missing=len(grid.ids))
            )
            if status == "provisional":
                session.execute(
                    insert(EvaluationCell),
                    [
                        {
                            "evaluation_id": identity,
                            "cell_id": cell,
                            "observed_count": count,
                            "outcome": "TP" if count else "FP",
                        }
                        for cell, count in counts.items()
                    ],
                )
            session.add(
                Evaluation(
                    id=identity,
                    forecast_id=forecast.id,
                    snapshot_id=observation.id,
                    version=version,
                    status=status,
                    reason="Pairing acceptance fixture",
                    metrics=metrics,
                )
            )
        return identity

    def publish(
        *,
        algorithm="candidate",
        created_at=PUBLISHED,
        evaluate=True,
        start=START,
        end=None,
        mode="synthetic_demo",
        **evaluation_changes,
    ):
        end = start + timedelta(days=1) if end is None else end
        with transaction() as session:
            inputs = snapshot(session)
            model = Model(
                id=str(uuid4()),
                experiment_id=experiment.id,
                algorithm=algorithm,
                status="available",
                training_cutoff=start - timedelta(days=1),
                available_at=start - timedelta(days=1),
                snapshot_id=inputs.id,
                metadata_={},
            )
            session.add(model)
            session.flush()
            forecast = Forecast(
                id=str(uuid4()),
                created_at=created_at,
                logical_key=uuid4().hex,
                experiment_id=experiment.id,
                model_id=model.id,
                snapshot_id=inputs.id,
                issued_at=created_at,
                start=start,
                end=end,
                input_cutoff=start - timedelta(days=1),
                mode=mode,
                status="published",
                provenance={"grid_hash": grid.hash},
            )
            session.execute(
                insert(ForecastCell),
                [
                    {"forecast_id": forecast.id, "cell_id": cell, "probability": 0.2, "band": "red"}
                    for cell in grid.ids
                ],
            )
            session.add(forecast)
            session.flush()
        if evaluate:
            append_evaluation(forecast, **evaluation_changes)
        return forecast

    return experiment, publish, append_evaluation


@pytest.mark.integration
def test_metrics_endpoint_loads_actual_cell_evidence_and_counts_latest_unavailable_forecasts(
    archived_comparison,
):
    experiment, publish, append_evaluation = archived_comparison
    foreign = publish(
        algorithm="historical_rate", created_at=PUBLISHED - timedelta(hours=2), foreign_cell=True
    )
    baseline = publish(algorithm="historical_rate")
    publish(algorithm="historical_rate", created_at=PUBLISHED + timedelta(seconds=1))
    candidate = publish()
    not_evaluated = publish(evaluate=False)
    awaiting = publish()
    latest_evaluation_id = append_evaluation(awaiting, status="awaiting_data", version=2)

    with TestClient(api.app) as client:
        response = client.get(
            "/metrics", params={"experiment_id": experiment.id, "mode": "synthetic_demo", "days": 0}
        )
    assert response.status_code == 200, response.text
    report = response.json()
    selected = decision(report, candidate.id)
    assert selected["status"] == "paired", selected
    assert selected["reference_forecast_id"] == baseline.id
    assert selected["reference_model_id"] == baseline.model_id
    assert decision(report, foreign.id)["status"] == "excluded"
    assert decision(report, not_evaluated.id)["reasons"] == ["candidate_not_evaluated"]
    latest = decision(report, awaiting.id)
    assert latest["evaluation_id"] == latest_evaluation_id and latest["evaluation_version"] == 2
    assert latest["reasons"] == ["candidate_observations_unavailable"]
    for forecast in (not_evaluated, awaiting):
        row = comparison(report, forecast.model_id)
        assert row["total_forecasts"] == 1 and row["excluded_days"] == 1 and row["paired_days"] == 0


@pytest.mark.integration
def test_metrics_endpoint_rejects_foreign_source_snapshot_with_identical_empty_revisions(
    archived_comparison,
):
    experiment, publish, _ = archived_comparison
    foreign = publish(algorithm="historical_rate", foreign_source=True)
    candidate = publish()
    params = {"experiment_id": experiment.id, "mode": "synthetic_demo", "days": 0}
    with TestClient(api.app) as client:
        response = client.get("/metrics", params=params)
        assert response.status_code == 200, response.text
        report = response.json()
        assert decision(report, foreign.id)["reasons"] == ["candidate_evidence_invalid"]
        assert decision(report, candidate.id)["reasons"] == ["reference_evidence_invalid"]

        # Matching empty revision sets are legitimate once both snapshots belong
        # to this experiment's source. The older foreign-source row stays excluded.
        valid = publish(algorithm="historical_rate", created_at=PUBLISHED + timedelta(seconds=1))
        response = client.get("/metrics", params=params)
    assert response.status_code == 200, response.text
    selected = decision(response.json(), candidate.id)
    assert selected["status"] == "paired" and selected["reference_forecast_id"] == valid.id
