"""Deterministic, evidence-matched comparisons over immutable forecast records.

The reference is the earliest *archived* compatible historical-rate forecast
for each exact window. Its score never participates in selection. Rolling folds
may therefore use different reference model versions; every choice is returned.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import pairwise
from math import isfinite

from .artifacts import digest
from .metrics import aggregate, brier_skill, paired_block_interval
from .timeutil import utc

POLICY_VERSION = "earliest-compatible-historical-rate-v1"
MATCHING_POLICY = (
    "Earliest archived compatible historical_rate reference per exact window, ordered by "
    "forecast.created_at, model_id, forecast_id; same experiment, start/end, mode, grid, "
    "observation revision set, eligible cell IDs and binary labels; self-reference allowed."
)


def _evidence(record):
    """Validate and canonicalize observation evidence without inspecting scores for selection."""
    if record["evaluation_id"] is None:
        return "not_evaluated", None, None, None
    metrics = record["metrics"]
    if record["status"] == "awaiting_data" or not metrics or metrics.get("n") == 0:
        return "observations_unavailable", None, None, None
    n, missing = metrics.get("n"), metrics.get("missing")
    revisions, observations = record["revision_ids"], record["observations"]
    if (
        record["status"] != "provisional"
        or type(n) is not int
        or n < 0
        or type(missing) is not int
        or missing < 0
        or not record.get("grid_hash")
        or record.get("evidence_valid") is False
        or revisions is None
        or not all(isinstance(item, str) and item for item in revisions)
        or observations is None
        or len(observations) != n
    ):
        return "evidence_invalid", None, None, None
    cells = {}
    for cell_id, label in observations:
        if not isinstance(cell_id, str) or not cell_id or type(label) is not int or label not in (0, 1):
            return "evidence_invalid", None, None, None
        if cell_id in cells:
            return "evidence_invalid", None, None, None
        cells[cell_id] = label
    grid_ids = record.get("grid_cell_ids")
    forecast_ids = record.get("forecast_cell_ids")
    if (
        (grid_ids is not None and (n + missing != len(grid_ids) or not cells.keys() <= set(grid_ids)))
        or (forecast_ids is not None and not cells.keys() <= set(forecast_ids))
        or not isinstance(metrics.get("brier"), (int, float))
        or not isfinite(metrics["brier"])
    ):
        return "evidence_invalid", None, None, None
    return (
        None,
        digest(sorted(set(revisions))),
        digest(sorted(cells)),
        digest(sorted(cells.items())),
    )


def _normalize(record):
    normalized = {
        **record,
        "start": utc(record["start"]),
        "end": utc(record["end"]),
        "forecast_created_at": utc(record["forecast_created_at"]),
    }
    reason, revisions, cells, observations = _evidence(normalized)
    # Snapshot lineage remains useful even when coverage is unavailable. Preserve
    # the legacy series snapshot digest independently of pairing eligibility.
    revision_ids = record["revision_ids"]
    if revision_ids is not None and all(isinstance(item, str) and item for item in revision_ids):
        revisions = digest(sorted(set(revision_ids)))
    normalized.update(
        evidence_reason=reason,
        revision_signature=revisions,
        cell_signature=cells,
        observation_signature=observations,
    )
    return normalized


def _reference_order(record):
    return record["forecast_created_at"], record["model_id"], record["forecast_id"]


def _mismatches(candidate, reference):
    reasons = []
    for key, reason in (
        ("experiment_id", "experiment_mismatch"),
        ("mode", "mode_mismatch"),
        ("end", "window_mismatch"),
        ("grid_hash", "grid_mismatch"),
    ):
        if candidate[key] != reference[key]:
            reasons.append(reason)
    if reference["evidence_reason"]:
        reasons.append(f"reference_{reference['evidence_reason']}")
    else:
        for key, reason in (
            ("revision_signature", "revision_mismatch"),
            ("cell_signature", "cell_mismatch"),
            ("observation_signature", "label_mismatch"),
        ):
            # A different cell set already explains why cell-label pairs differ.
            if key == "observation_signature" and candidate["cell_signature"] != reference["cell_signature"]:
                continue
            if candidate[key] != reference[key]:
                reasons.append(reason)
    return reasons


def _decision(candidate, references):
    result = {
        "forecast_id": candidate["forecast_id"],
        "model_id": candidate["model_id"],
        "evaluation_id": candidate["evaluation_id"],
        "evaluation_version": candidate["evaluation_version"],
        "experiment_id": candidate["experiment_id"],
        "start": candidate["start"].isoformat(),
        "end": candidate["end"].isoformat(),
        "mode": candidate["mode"],
        "grid_hash": candidate["grid_hash"],
        "revision_signature": candidate["revision_signature"],
        "cell_signature": candidate["cell_signature"],
        "observation_signature": candidate["observation_signature"],
        "status": "excluded",
        "reason": None,
        "reasons": [],
        "reference_forecast_id": None,
        "reference_model_id": None,
        "reference_evaluation_id": None,
        "reference_evaluation_version": None,
        "reference_created_at": None,
        "self_reference": False,
        "reference_rejections": [],
    }
    if candidate["evidence_reason"]:
        result["reasons"] = [f"candidate_{candidate['evidence_reason']}"]
    elif not references:
        result["reasons"] = ["reference_not_found"]
    else:
        for reference in references:
            reasons = _mismatches(candidate, reference)
            if reasons:
                result["reference_rejections"].append(
                    {"forecast_id": reference["forecast_id"], "reasons": reasons}
                )
                continue
            result.update(
                status="paired",
                reference_forecast_id=reference["forecast_id"],
                reference_model_id=reference["model_id"],
                reference_evaluation_id=reference["evaluation_id"],
                reference_evaluation_version=reference["evaluation_version"],
                reference_created_at=reference["forecast_created_at"].isoformat(),
                self_reference=candidate["forecast_id"] == reference["forecast_id"],
            )
            return result, reference
        result["reasons"] = sorted(
            {reason for rejection in result["reference_rejections"] for reason in rejection["reasons"]}
        )
    result["reason"] = result["reasons"][0]
    return result, None


def build_comparison_report(records):
    """Return legacy scores plus explicit decisions for every forecast, even unevaluated ones.

    Records contain raw ``revision_ids`` and ``observations`` (cell ID, binary
    label pairs); neither their ordering nor snapshot IDs are matching criteria.
    Optional grid/forecast cell ID sets validate archived evidence membership.
    The database adapter always supplies these immutable sets.
    """
    records = sorted(
        (_normalize(record) for record in records),
        key=lambda r: (r["start"], r["end"], r["model_id"], r["forecast_id"]),
    )
    references = defaultdict(list)
    models = defaultdict(list)
    for record in records:
        models[record["model_id"]].append(record)
        if record["algorithm"] == "historical_rate":
            references[record["start"]].append(record)
    for candidates in references.values():
        candidates.sort(key=_reference_order)

    comparisons, decisions = [], []
    for model_id, series in sorted(models.items()):
        paired, model_decisions = [], []
        for record in series:
            decision, reference = _decision(record, references[record["start"]])
            model_decisions.append(decision)
            if reference is not None:
                paired.append((record, reference))
        decisions.extend(model_decisions)
        model_score = aggregate([candidate["metrics"] for candidate, _ in paired])
        reference_score = aggregate([reference["metrics"] for _, reference in paired])
        # Preserve the existing consecutive-day uncertainty calculation over the
        # exact references selected above.
        dates = [candidate["start"] for candidate, _ in paired]
        contiguous = all(b - a == timedelta(days=1) for a, b in pairwise(dates))
        uncertainty = (
            paired_block_interval(
                [
                    candidate["metrics"]["brier"] - reference["metrics"]["brier"]
                    for candidate, reference in paired
                ]
            )
            if contiguous
            else {"status": "insufficient_data", "reason": "Nonconsecutive eligible days"}
        )
        reference_models = sorted({reference["model_id"] for _, reference in paired})
        reference_forecasts = sorted({reference["forecast_id"] for _, reference in paired})
        reference_evaluations = sorted({reference["evaluation_id"] for _, reference in paired})
        comparisons.append(
            {
                "model_id": model_id,
                "algorithm": series[0]["algorithm"],
                "metrics": aggregate([r["metrics"] for r in series if r["metrics"] is not None]),
                "paired_metrics": model_score,
                "reference_metrics": reference_score,
                "brier_skill": brier_skill(model_score["brier"], reference_score["brier"]),
                "paired_days": len(paired),
                "excluded_days": len(series) - len(paired),
                "total_forecasts": len(series),
                "evaluated_forecasts": sum(r["evaluation_id"] is not None for r in series),
                "uncertainty": uncertainty,
                "reference_model_ids": reference_models,
                "reference_model_count": len(reference_models),
                "reference_forecast_ids": reference_forecasts,
                "reference_forecast_count": len(reference_forecasts),
                "reference_evaluation_ids": reference_evaluations,
                "reference_evaluation_count": len(reference_evaluations),
                "mixed_reference_models": len(reference_models) > 1,
                "exclusion_reasons": dict(
                    sorted(Counter(reason for d in model_decisions for reason in d["reasons"]).items())
                ),
                "decisions": model_decisions,
            }
        )
    return {
        "policy_version": POLICY_VERSION,
        "matching_policy": MATCHING_POLICY,
        "series": [
            {
                "date": r["start"].isoformat(),
                "start": r["start"].isoformat(),
                "end": r["end"].isoformat(),
                "forecast_id": r["forecast_id"],
                "model_id": r["model_id"],
                "algorithm": r["algorithm"],
                "evaluation_id": r["evaluation_id"],
                "evaluation_version": r["evaluation_version"],
                "status": r["status"],
                "snapshot": r["revision_signature"],
                "metrics": r["metrics"],
            }
            for r in records
            if r["evaluation_id"] is not None
        ],
        "comparisons": comparisons,
        "decisions": decisions,
    }
