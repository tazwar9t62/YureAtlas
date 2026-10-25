"""Rejected-reference examples stay bounded without truncating scientific selection."""

import json
from datetime import timedelta
from random import Random

import pytest
from test_comparison_pairing import PUBLISHED, comparison, decision, record, reference
from yureatlas.comparisons import build_comparison_report
from yureatlas.reports import MAX_FORECASTS
from yureatlas.schemas import PairDecision


def rejected_references(count):
    return [
        reference(
            f"rejected-{index:04d}",
            revision_ids=[f"old-revision-{index}"],
            forecast_created_at=PUBLISHED + timedelta(seconds=index),
        )
        for index in range(count)
    ]


def test_reference_after_example_cap_is_selected_and_only_examined_rejections_are_counted():
    candidate = record("candidate")
    chosen = reference("chosen", forecast_created_at=PUBLISHED + timedelta(seconds=25))
    later = reference(
        "never-examined",
        revision_ids=["other"],
        forecast_created_at=PUBLISHED + timedelta(seconds=26),
    )
    report = build_comparison_report([candidate, *rejected_references(25), chosen, later])
    selected = decision(report, "candidate")
    assert selected["reference_forecast_id"] == "chosen"
    assert selected["status"] == "paired"
    assert selected["reason"] is None and selected["reasons"] == []
    assert selected["reference_rejection_limit"] == 20
    assert selected["reference_rejection_count"] == 25
    assert selected["reference_rejections_omitted"] == 5
    assert selected["reference_rejection_reasons"] == {"revision_mismatch": 25}
    assert [item["forecast_id"] for item in selected["reference_rejections"]] == [
        f"rejected-{index:04d}" for index in range(20)
    ]
    assert PairDecision.model_validate(selected).model_dump(mode="json")["reference_rejection_count"] == 25
    without_rejections = comparison(build_comparison_report([candidate, chosen]), "model-candidate")
    actual = comparison(report, "model-candidate")
    for key in ("metrics", "paired_metrics", "reference_metrics", "brier_skill", "uncertainty"):
        assert actual[key] == without_rejections[key]


def test_omitted_reference_preserves_late_only_reasons_and_overlapping_reason_counts():
    records = [
        record("candidate"),
        *rejected_references(20),
        reference(
            "late-only-reasons",
            forecast_created_at=PUBLISHED + timedelta(seconds=20),
            grid_hash="other-grid",
            observations=[("cell-a", 1), ("cell-b", 0)],
        ),
    ]
    expected = build_comparison_report(records)
    selected = decision(expected, "candidate")
    assert selected["status"] == "excluded"
    assert selected["reference_forecast_id"] is None
    assert selected["reference_rejection_count"] == 21
    assert selected["reference_rejections_omitted"] == 1
    assert selected["reference_rejection_reasons"] == {
        "grid_mismatch": 1,
        "label_mismatch": 1,
        "revision_mismatch": 20,
    }
    assert selected["reason"] == "grid_mismatch"
    assert selected["reasons"] == ["grid_mismatch", "label_mismatch", "revision_mismatch"]
    assert all(item["reasons"] == ["revision_mismatch"] for item in selected["reference_rejections"])
    assert comparison(expected, "model-candidate")["exclusion_reasons"] == {
        "grid_mismatch": 1,
        "label_mismatch": 1,
        "revision_mismatch": 1,
    }
    rng = Random(2026)
    for _ in range(5):
        rng.shuffle(records)
        assert build_comparison_report(records) == expected


@pytest.mark.parametrize(
    "records, forecast_id",
    [
        ([record("no-reference")], "no-reference"),
        ([record("unavailable", evaluation_id=None), reference("reference")], "unavailable"),
        ([reference("self")], "self"),
    ],
)
def test_no_examined_rejections_has_explicit_zero_diagnostics(records, forecast_id):
    selected = decision(build_comparison_report(records), forecast_id)
    assert selected["reference_rejections"] == []
    assert selected["reference_rejection_count"] == selected["reference_rejections_omitted"] == 0
    assert selected["reference_rejection_reasons"] == {}


def test_maximum_forecast_report_bounds_serialized_diagnostics():
    # Every baseline rejects its predecessors; other models reject every baseline.
    # Distinct revision sets are legitimate archived evidence, not corrupt rows.
    reference_count = MAX_FORECASTS * 2 // 3
    records = [
        record(
            f"forecast-{index:04d}",
            algorithm="historical_rate" if index < reference_count else "candidate",
            revision_ids=[f"revision-{index}"],
        )
        for index in range(MAX_FORECASTS)
    ]
    report = build_comparison_report(records)
    decisions = report["decisions"]
    expected_rejections = reference_count * (reference_count - 1) // 2 + reference_count * (
        MAX_FORECASTS - reference_count
    )
    assert sum(item["reference_rejection_count"] for item in decisions) == expected_rejections
    assert all(len(item["reference_rejections"]) <= 20 for item in decisions)
    assert all(
        item["reference_rejection_count"]
        == len(item["reference_rejections"]) + item["reference_rejections_omitted"]
        for item in decisions
    )
    # Includes both report-level and per-model decisions, as the HTTP contract does.
    # Before the cap this small-cell archive emitted over 230 MB of diagnostics.
    encoded = json.dumps(report, separators=(",", ":"), allow_nan=False).encode()
    assert len(encoded) < 24_000_000
