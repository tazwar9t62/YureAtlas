"""Small independent arithmetic oracles for paired scientific summaries."""

import json
from copy import deepcopy
from datetime import timedelta
from itertools import permutations
from math import log, sqrt

import pytest
from yureatlas.comparisons import build_comparison_report
from yureatlas.metrics import aggregate, brier_skill, paired_score_interval, score
from yureatlas.timeutil import utc

START = utc("2021-07-01T15:00:00Z")


def forecast(identity, day, probabilities, labels, areas, *, reference=False, grid_cells=None, **changes):
    start = START + timedelta(days=day)
    cells = ["cell-a", "cell-b", "cell-c"] if grid_cells is None else grid_cells
    return {
        "forecast_id": identity,
        "forecast_created_at": start - timedelta(hours=1),
        "model_id": "reference-model" if reference else "candidate-model",
        "algorithm": "historical_rate" if reference else "candidate",
        "experiment_id": "experiment",
        "grid_hash": "grid",
        "grid_cell_ids": cells,
        "forecast_cell_ids": cells,
        "mode": "synthetic_demo",
        "start": start,
        "end": start + timedelta(days=1),
        "evaluation_id": "evaluation-" + identity,
        "evaluation_version": 1,
        "status": "provisional",
        "revision_ids": ["revision-" + str(day)],
        "observations": list(zip(cells, labels, strict=False)),
        "metrics": score(probabilities, labels, areas, threshold=0.5, missing=len(cells) - len(labels)),
        **changes,
    }


def exact_records():
    return [
        forecast("candidate-a", 0, [0.9], [1], [1]),
        forecast("candidate-b", 1, [0.2, 0.2, 0.2], [1, 0, 0], [1, 2, 3]),
        forecast("reference-a", 0, [0.5], [1], [1], reference=True),
        forecast("reference-b", 1, [0.5, 0.5, 0.5], [1, 0, 0], [1, 2, 3], reference=True),
    ]


def candidate_comparison(records):
    return next(
        row for row in build_comparison_report(records)["comparisons"] if row["model_id"] == "candidate-model"
    )


def assert_hand_calculated_scores(result):
    assert result["n"] == 4
    assert result["brier_sum"] == pytest.approx(0.73)
    assert result["brier"] == pytest.approx(0.1825)
    assert (result["tp"], result["fn"], result["tn"], result["fp"]) == (1, 1, 2, 0)
    assert result["positives"] == 2
    assert result["recall"] == 0.5 and result["precision"] == 1 and result["false_positive_rate"] == 0
    assert result["red_area"] == 1 and result["eligible_area"] == 7
    assert result["red_coverage"] == pytest.approx(1 / 7)
    # Product of the four assigned outcome probabilities: .9 * .2 * .8 * .8.
    assert result["log_loss_sum"] == pytest.approx(-log(0.9 * 0.2 * 0.8 * 0.8))
    assert result["log_loss"] == pytest.approx(-log(0.9 * 0.2 * 0.8 * 0.8) / 4)


def test_aggregate_matches_hand_calculation_and_missing_observations_do_not_become_negatives():
    first = score([0.9], [1], [1], threshold=0.5)
    second = score([0.2, 0.2, 0.2], [1, 0, 0], [1, 2, 3], threshold=0.5)
    known = aggregate([first, second])
    assert_hand_calculated_scores(known)
    assert known["brier"] != pytest.approx((first["brier"] + second["brier"]) / 2)
    unknown = score([], [], missing=2)
    assert aggregate([first, unknown, second]) == dict(known, missing=2)
    baseline = aggregate([score([0.5], [1]), score([0.5, 0.5, 0.5], [1, 0, 0])])
    assert baseline["brier"] == 0.25
    assert brier_skill(known["brier"], baseline["brier"]) == pytest.approx(0.27)


def test_paired_candidate_and_reference_aggregates_use_the_same_windows_in_every_order():
    records = exact_records()
    expected = candidate_comparison(records)
    for ordering in permutations(records):
        assert candidate_comparison(list(ordering)) == expected
    assert_hand_calculated_scores(expected["paired_metrics"])
    assert expected["reference_metrics"]["n"] == 4
    assert expected["reference_metrics"]["brier"] == 0.25
    assert expected["brier_skill"] == pytest.approx(0.27)
    assert expected["brier_skill_reason"] is None
    assert expected["paired_days"] == 2 and expected["excluded_days"] == 0
    assert expected["paired_cell_windows"] == 4 and expected["paired_missing_cell_windows"] == 2


def test_unpaired_perfect_and_wrong_forecasts_cannot_change_either_paired_score():
    records = exact_records()
    expected = candidate_comparison(records)
    records.extend(
        [
            forecast("wrong-unpaired", 2, [0, 1, 1], [1, 0, 0], [1, 2, 3]),
            forecast("incompatible-reference", 2, [1, 0, 0], [0, 1, 1], [1, 2, 3], reference=True),
            forecast("perfect-unpaired", 3, [1, 0, 0], [1, 0, 0], [1, 2, 3]),
            forecast("orphan-reference", 4, [1, 0, 0], [0, 1, 1], [1, 2, 3], reference=True),
        ]
    )
    actual = candidate_comparison(records)
    assert actual["paired_metrics"] == expected["paired_metrics"]
    assert actual["reference_metrics"] == expected["reference_metrics"]
    assert actual["brier_skill"] == expected["brier_skill"]
    assert actual["metrics"]["n"] == 10 and actual["paired_metrics"]["n"] == 4
    assert actual["paired_days"] == 2 and actual["excluded_days"] == 2
    assert actual["exclusion_reasons"] == {"label_mismatch": 1, "reference_not_found": 1}


def test_unavailable_window_preserves_missing_denominator_without_changing_performance():
    records = exact_records()
    records.append(forecast("awaiting", 2, [], [], [], status="awaiting_data"))
    result = candidate_comparison(records)
    assert_hand_calculated_scores(result["metrics"])
    assert result["metrics"]["missing"] == 5  # Two absent cells on day A, all three on the unknown day.
    assert result["paired_metrics"]["missing"] == 2
    assert result["paired_days"] == 2 and result["excluded_days"] == 1
    assert result["total_forecasts"] == result["evaluated_forecasts"] == 3


def test_report_denominators_distinguish_unknown_invalid_and_valid_unavailable_evidence():
    records = exact_records()
    records.extend(
        [
            forecast("awaiting", 2, [], [], [], status="awaiting_data"),
            forecast(
                "unevaluated",
                3,
                [],
                [],
                [],
                status="not_evaluated",
                evaluation_id=None,
                evaluation_version=None,
                metrics=None,
                observations=None,
                revision_ids=None,
            ),
            forecast("invalid", 4, [0], [1], [1], evidence_valid=False),
        ]
    )
    result = candidate_comparison(records)
    assert_hand_calculated_scores(result["metrics"])
    assert result["metrics"]["missing"] == 5
    assert result["total_forecasts"] == 5 and result["evaluated_forecasts"] == 4
    assert result["scoreable_forecasts"] == 2 and result["unavailable_forecasts"] == 3
    assert result["unevaluated_forecasts"] == 1
    assert result["unavailable_evaluation_forecasts"] == 1 and result["invalid_evidence_forecasts"] == 1
    assert result["observed_cell_windows"] == 4 and result["missing_cell_windows"] == 5
    assert result["paired_cell_windows"] == 4 and result["paired_missing_cell_windows"] == 2
    assert result["excluded_observed_cell_windows"] == 0 and result["excluded_missing_cell_windows"] == 3
    assert result["total_cell_windows"] == 15 and result["unscored_cell_windows"] == 11
    assert result["unknown_cell_window_forecasts"] == 0
    assert result["primary_exclusion_reasons"] == {
        "candidate_not_evaluated": 1,
        "candidate_observations_unavailable": 1,
        "candidate_evidence_invalid": 1,
    }
    assert sum(result["primary_exclusion_reasons"].values()) == result["excluded_days"] == 3


def test_missing_expected_grid_size_is_explicit_instead_of_an_invented_total():
    candidate = forecast("candidate", 0, [0.9], [1], [1])
    candidate.pop("grid_cell_ids")
    result = candidate_comparison([candidate, forecast("reference", 0, [0.5], [1], [1], reference=True)])
    assert result["unknown_cell_window_forecasts"] == 1
    assert result["total_cell_windows"] is None and result["unscored_cell_windows"] is None
    assert result["observed_cell_windows"] == 1 and result["missing_cell_windows"] == 2


def test_internally_valid_score_with_labels_contradicting_cell_evidence_is_not_aggregated():
    candidate = forecast("candidate", 0, [0.9], [1], [1], observations=[("cell-a", 0)])
    reference = forecast("reference", 0, [0.5], [0], [1], reference=True)
    # The stored metrics describe one positive and have coherent TP/FN totals,
    # but the archived cell says there was no event. Counts alone cannot validate it.
    assert candidate["metrics"]["positives"] == candidate["metrics"]["tp"] == 1
    result = candidate_comparison([candidate, reference])
    assert result["decisions"][0]["reasons"] == ["candidate_evidence_invalid"]
    assert result["invalid_evidence_forecasts"] == 1 and result["scoreable_forecasts"] == 0
    assert result["metrics"]["n"] == 0 and result["metrics"]["brier"] is None
    assert result["paired_cell_windows"] == 0 and result["unscored_cell_windows"] == 3


@pytest.mark.parametrize("invalid", ["source", "nonfinite_sum"])
def test_invalid_evaluation_stays_in_decisions_but_not_scored_history_or_nonfinite_json(invalid):
    candidate = forecast("invalid", 0, [0.9], [1], [1])
    if invalid == "source":
        candidate["evidence_valid"] = False
    else:
        candidate["metrics"]["brier_sum"] = float("nan")
    report = build_comparison_report([candidate, forecast("reference", 0, [0.5], [1], [1], reference=True)])
    assert all(row["forecast_id"] != "invalid" for row in report["series"])
    decision = next(row for row in report["decisions"] if row["forecast_id"] == "invalid")
    assert decision["evaluation_id"] == "evaluation-invalid"
    assert decision["reasons"] == ["candidate_evidence_invalid"]
    counts = next(row for row in report["comparisons"] if row["model_id"] == "candidate-model")
    assert counts["evaluated_forecasts"] == counts["invalid_evidence_forecasts"] == 1
    assert counts["scoreable_forecasts"] == 0
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("bad_optional", ["optional_extra", "calibration"])
def test_history_recomputes_ratios_and_filters_invalid_optional_fields(bad_optional):
    candidate = forecast("candidate", 0, [0.9], [1], [1])
    original = deepcopy(candidate["metrics"])
    candidate["metrics"].update(
        brier=0.99,
        log_loss=999,
        recall=0,
        precision=-1,
        false_positive_rate=1,
        red_coverage=0,
    )
    candidate["metrics"][bad_optional] = [{"nested": [1, float("inf")]}]
    report = build_comparison_report([candidate, forecast("reference", 0, [0.5], [1], [1], reference=True)])
    history = next(row["metrics"] for row in report["series"] if row["forecast_id"] == "candidate")
    for field in ("brier", "log_loss", "recall", "precision", "false_positive_rate", "red_coverage"):
        assert history[field] == original[field]
    if bad_optional != "calibration":
        assert history["calibration"] == original["calibration"]
    assert bad_optional not in history
    # Report construction does not rewrite the immutable stored daily payload.
    assert candidate["metrics"]["brier"] == 0.99
    json.dumps(report, allow_nan=False)


def test_no_pair_and_perfect_reference_have_distinct_unavailable_skill_reasons():
    no_pairs = candidate_comparison([forecast("candidate", 0, [0.2], [1], [1])])
    assert no_pairs["brier_skill"] is None and no_pairs["brier_skill_reason"] == "no_pairs"
    assert no_pairs["paired_cell_windows"] == 0
    perfect = candidate_comparison(
        [
            forecast("candidate", 0, [0.2, 0.2, 0.2], [1, 0, 0], [1, 2, 3]),
            forecast("reference", 0, [1, 0, 0], [1, 0, 0], [1, 2, 3], reference=True),
        ]
    )
    assert perfect["paired_days"] == 1 and perfect["reference_metrics"]["brier"] == 0
    assert perfect["brier_skill"] is None and perfect["brier_skill_reason"] == "reference_brier_zero"


def intervals(days=28):
    return [
        {
            "start": START + timedelta(days=day),
            "end": START + timedelta(days=day + 1),
            "candidate_sum": 0.6,
            "reference_sum": 0.3,
            "candidate_n": 3,
            "reference_n": 3,
        }
        for day in range(days)
    ]


def test_uncertainty_requires_28_days_and_preserves_constant_paired_difference():
    short = paired_score_interval(intervals(27))
    assert short["status"] == "insufficient_data" and short["reason"] == "insufficient_days"
    assert short["days"] == 27 and short["minimum_days"] == 28
    enough = paired_score_interval(intervals())
    assert enough["status"] == "available" and enough["reason"] is None
    assert enough["mean_difference"] == pytest.approx(0.1)
    assert enough["interval95"] == pytest.approx([0.1, 0.1])
    assert enough["days"] == 28 and enough["paired_cell_windows"] == 84


@pytest.mark.parametrize("problem", ["gap", "duplicate", "short_window", "long_window"])
def test_uncertainty_refuses_invalid_time_blocks_with_explicit_reason(problem):
    pairs = intervals()
    if problem == "gap":
        for pair in pairs[14:]:
            pair["start"] += timedelta(days=1)
            pair["end"] += timedelta(days=1)
        reason = "nonconsecutive_windows"
    elif problem == "duplicate":
        pairs[14] = dict(pairs[13])
        reason = "duplicate_windows"
    else:
        pairs[14]["end"] = pairs[14]["start"] + timedelta(hours=12 if problem == "short_window" else 48)
        reason = "non_24h_windows"
    result = paired_score_interval(pairs)
    assert result["status"] == "insufficient_data" and result["reason"] == reason
    assert result.get("interval95") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_sum": float("nan")},
        {"reference_sum": float("inf")},
        {"candidate_sum": -0.1},
        {"reference_sum": 3.1},
        {"candidate_sum": "0.6"},
        {"candidate_sum": True},
        {"candidate_n": 0, "reference_n": 0},
        {"candidate_n": -1, "reference_n": -1},
        {"candidate_n": 3.0},
        {"reference_n": True},
        {"reference_n": 2},
    ],
)
def test_invalid_scores_or_weights_cannot_produce_an_available_interval(changes):
    pairs = intervals()
    pairs[10].update(changes)
    result = paired_score_interval(pairs)
    assert result["status"] == "insufficient_data" and result["reason"] == "invalid_scores"
    assert result.get("interval95") is None


@pytest.mark.parametrize("changes", [{"start": "not-a-time"}, {"end": None}])
def test_malformed_window_timestamps_have_an_explicit_unavailable_reason(changes):
    pairs = intervals()
    pairs[10].update(changes)
    result = paired_score_interval(pairs)
    assert result["status"] == "insufficient_data" and result["reason"] == "invalid_windows"


@pytest.mark.parametrize(
    "config",
    [{"block_days": 0}, {"block_days": True}, {"simulations": 0}, {"simulations": 2.5}, {"seed": -1}],
)
def test_invalid_uncertainty_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        paired_score_interval(intervals(), **config)


def weighted_pairs():
    pairs = intervals()
    for day, pair in enumerate(pairs):
        pair.update(
            candidate_sum=0 if day % 2 else 0.2,
            reference_sum=0.9 if day % 2 else 0,
            candidate_n=9 if day % 2 else 1,
            reference_n=9 if day % 2 else 1,
        )
    return pairs


def test_weighted_interval_matches_independent_seven_day_block_oracle_and_input_permutations():
    pairs = weighted_pairs()
    result = paired_score_interval(pairs)
    # Fourteen light days contribute +.2 each; fourteen heavy days contribute -.9.
    assert result["mean_difference"] == pytest.approx((14 * 0.2 - 14 * 0.9) / (14 + 14 * 9))
    assert result["mean_difference"] == pytest.approx(-0.07)
    assert result["mean_difference"] != pytest.approx((0.2 - 0.1) / 2)
    assert result["paired_cell_windows"] == 140
    # A seven-day even-start block has four light and three heavy days: sum=-1.9,n=31.
    # An odd-start block has three light and four heavy days: sum=-3,n=39.
    # Four blocks therefore yield (-7.6-1.1*k)/(124+8*k), k=0..4. With the fixed
    # 1,000-draw seed, both endpoint masses exceed 2.5%; these are the exact bounds.
    assert result["interval95"] == pytest.approx([-12 / 156, -7.6 / 124])
    for ordering in (list(reversed(pairs)), pairs[14:] + pairs[:14], pairs[::2] + pairs[1::2]):
        assert paired_score_interval(ordering) == result


def test_comparison_uncertainty_uses_the_same_pooled_cell_weights_as_reported_scores():
    records = []
    grid = ["cell-" + str(index) for index in range(9)]
    for day in range(28):
        n = 9 if day % 2 else 1
        for reference in (False, True):
            probability = (sqrt(0.1) if reference else 0) if day % 2 else (0 if reference else sqrt(0.2))
            records.append(
                forecast(
                    ("reference-" if reference else "candidate-") + str(day),
                    day,
                    [probability] * n,
                    [0] * n,
                    [1] * n,
                    reference=reference,
                    grid_cells=grid,
                )
            )
    result = candidate_comparison(records)
    assert result["paired_cell_windows"] == 140 and result["paired_missing_cell_windows"] == 112
    assert result["paired_metrics"]["brier"] == pytest.approx(0.02)
    assert result["reference_metrics"]["brier"] == pytest.approx(0.09)
    assert result["uncertainty"]["mean_difference"] == pytest.approx(-0.07)
    assert result["uncertainty"]["mean_difference"] == pytest.approx(
        result["paired_metrics"]["brier"] - result["reference_metrics"]["brier"]
    )
    assert result["uncertainty"]["interval95"] == pytest.approx([-12 / 156, -7.6 / 124])
    assert candidate_comparison(list(reversed(records))) == result
