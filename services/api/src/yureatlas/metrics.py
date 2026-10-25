"""Scores for binary cell-window occurrence, with explicit denominators."""

from datetime import timedelta
from itertools import pairwise
from math import isfinite

import numpy as np
from sklearn.metrics import average_precision_score

from .timeutil import utc

EPSILON = 1e-12
COUNT_FIELDS = ("n", "missing", "positives", "tp", "fp", "tn", "fn")
SUM_FIELDS = ("brier_sum", "log_loss_sum", "red_area", "eligible_area")


def score(probability, observed, areas=None, threshold=0.05, missing=0):
    p, y = np.asarray(probability, dtype=float), np.asarray(observed, dtype=int)
    if p.shape != y.shape or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError("Probabilities must be finite, bounded and aligned with labels")
    if not np.isin(y, [0, 1]).all():
        raise ValueError("Binary labels required")
    area = np.ones(len(p)) if areas is None else np.asarray(areas, dtype=float)
    red = p >= threshold
    tp, fp = int(np.sum(red & (y == 1))), int(np.sum(red & (y == 0)))
    fn, tn = int(np.sum(~red & (y == 1))), int(np.sum(~red & (y == 0)))
    n = len(p)
    errors = (p - y) ** 2
    clipped = np.clip(p, EPSILON, 1 - EPSILON)
    losses = -(y * np.log(clipped) + (1 - y) * np.log1p(-clipped))
    edges = np.array([0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0000001])
    bins = []
    for lo, hi in pairwise(edges):
        selected = (p >= lo) & (p < hi)
        count = int(selected.sum())
        bins.append(
            {
                "lower": float(lo),
                "upper": min(float(hi), 1),
                "count": count,
                "predicted": float(p[selected].mean()) if count else None,
                "observed": float(y[selected].mean()) if count else None,
            }
        )
    return {
        "n": n,
        "missing": missing,
        "positives": int(y.sum()),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "brier": float(errors.mean()) if n else None,
        "log_loss": float(losses.mean()) if n else None,
        "brier_sum": float(errors.sum()),
        "log_loss_sum": float(losses.sum()),
        "recall": tp / (tp + fn) if tp + fn else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "red_coverage": float(area[red].sum() / area.sum()) if area.sum() else None,
        "red_area": float(area[red].sum()),
        "eligible_area": float(area.sum()),
        "average_precision": float(average_precision_score(y, p))
        if y.sum() >= 5 and (y == 0).any()
        else None,
        "calibration": bins,
        "clipping_epsilon": EPSILON,
        "confident_misses": int(np.sum((p == 0) & (y == 1))),
        "threshold": threshold,
    }


def valid_totals(metrics):
    """Whether stored totals can safely contribute to pooled scores.

    Derived daily percentages are deliberately ignored: pooled results are
    defined by these counts and sums, including valid zero-observation rows.
    """
    if not isinstance(metrics, dict):
        return False
    if any(type(metrics.get(key)) is not int or metrics[key] < 0 for key in COUNT_FIELDS):
        return False
    if any(
        type(metrics.get(key)) not in (int, float) or not isfinite(metrics[key]) or metrics[key] < 0
        for key in SUM_FIELDS
    ):
        return False
    n = metrics["n"]
    return (
        metrics["tp"] + metrics["fp"] + metrics["tn"] + metrics["fn"] == n
        and metrics["positives"] == metrics["tp"] + metrics["fn"]
        and metrics["brier_sum"] <= n
        and metrics["red_area"] <= metrics["eligible_area"]
        and (n > 0 or all(metrics[key] == 0 for key in SUM_FIELDS))
    )


def aggregate(metrics):
    metrics = list(metrics)
    if not all(valid_totals(row) for row in metrics):
        raise ValueError("Pooled metrics require valid finite sums and nonnegative integer counts")
    result = {key: sum(m[key] for m in metrics) for key in (*COUNT_FIELDS, *SUM_FIELDS)}
    n, tp, fp, fn, tn = (result[k] for k in ("n", "tp", "fp", "fn", "tn"))
    result.update(
        brier=result["brier_sum"] / n if n else None,
        log_loss=result["log_loss_sum"] / n if n else None,
        recall=tp / (tp + fn) if tp + fn else None,
        precision=tp / (tp + fp) if tp + fp else None,
        false_positive_rate=fp / (fp + tn) if fp + tn else None,
        red_coverage=result["red_area"] / result["eligible_area"] if result["eligible_area"] else None,
    )
    return result


def brier_skill(candidate, baseline):
    return (
        1 - candidate / baseline if baseline is not None and baseline > 0 and candidate is not None else None
    )


def paired_score_interval(pairs, block_days=7, simulations=1000, seed=2026):
    """Resample consecutive-day blocks of paired Brier loss sums and denominators.

    Each pair contains start/end, candidate_sum/reference_sum, and
    candidate_n/reference_n. A shared positive denominator is required within
    each window; it may vary between windows. Missing observations never enter
    the loss denominator. This remains an experimental uncertainty estimate.
    """
    if any(type(value) is not int or value <= 0 for value in (block_days, simulations)):
        raise ValueError("Block length and simulation count must be positive integers")
    if type(seed) is not int or seed < 0:
        raise ValueError("Seed must be a nonnegative integer")
    pairs = list(pairs)
    result = {
        "status": "insufficient_data",
        "reason": None,
        "days": len(pairs),
        "minimum_days": 4 * block_days,
        "block_days": block_days,
        "simulations": simulations,
        "seed": seed,
        "paired_cell_windows": 0,
        "mean_difference": None,
        "interval95": None,
        "method_version": "paired-weighted-moving-blocks-v1",
        "estimand": "pooled candidate Brier minus pooled reference Brier",
        "policy": "paired moving consecutive-day blocks; ratio of resampled candidate-minus-reference loss sums to paired eligible cell-window counts; experimental uncertainty estimate",
    }
    windows = []
    for pair in pairs:
        counts = [pair.get("candidate_n"), pair.get("reference_n")]
        sums = [pair.get("candidate_sum"), pair.get("reference_sum")]
        if (
            any(type(n) is not int or n <= 0 for n in counts)
            or counts[0] != counts[1]
            or any(
                type(value) not in (int, float) or not isfinite(value) or value < 0 or value > counts[0]
                for value in sums
            )
        ):
            return {**result, "reason": "invalid_scores"}
        try:
            start, end = utc(pair["start"]), utc(pair["end"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return {**result, "reason": "invalid_windows"}
        windows.append((start, end, sums[0] - sums[1], counts[0]))
    windows.sort(key=lambda window: (window[0], window[1]))
    result["paired_cell_windows"] = sum(window[3] for window in windows)
    if any(end - start != timedelta(days=1) for start, end, _, _ in windows):
        return {**result, "reason": "non_24h_windows"}
    if len({start for start, _, _, _ in windows}) != len(windows):
        return {**result, "reason": "duplicate_windows"}
    if any(first[1] != second[0] for first, second in pairwise(windows)):
        return {**result, "reason": "nonconsecutive_windows"}
    if len(windows) < 4 * block_days:
        return {**result, "reason": "insufficient_days"}
    differences = np.asarray([window[2] for window in windows], dtype=float)
    counts = np.asarray([window[3] for window in windows], dtype=float)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(simulations):
        starts = rng.integers(0, len(windows) - block_days + 1, size=int(np.ceil(len(windows) / block_days)))
        indices = np.concatenate([np.arange(start, start + block_days) for start in starts])[: len(windows)]
        means.append(differences[indices].sum() / counts[indices].sum())
    return {
        **result,
        "status": "available",
        "mean_difference": float(differences.sum() / counts.sum()),
        "interval95": np.quantile(means, [0.025, 0.975]).tolist(),
    }
