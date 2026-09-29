"""Scores for binary cell-window occurrence, with explicit denominators."""

from itertools import pairwise

import numpy as np
from sklearn.metrics import average_precision_score

EPSILON = 1e-12


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


def aggregate(metrics):
    result = {
        key: sum(m[key] for m in metrics)
        for key in (
            "n",
            "missing",
            "positives",
            "tp",
            "fp",
            "tn",
            "fn",
            "brier_sum",
            "log_loss_sum",
            "red_area",
            "eligible_area",
        )
    }
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


def paired_block_interval(daily_differences, block_days=7, simulations=1000, seed=2026):
    values = np.asarray(daily_differences, dtype=float)
    if len(values) < 4 * block_days:
        return {"status": "insufficient_data", "days": len(values), "minimum_days": 4 * block_days}
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(simulations):
        starts = rng.integers(0, len(values) - block_days + 1, size=int(np.ceil(len(values) / block_days)))
        sample = np.concatenate([values[s : s + block_days] for s in starts])[: len(values)]
        means.append(sample.mean())
    return {
        "status": "available",
        "mean_difference": float(values.mean()),
        "interval95": np.quantile(means, [0.025, 0.975]).tolist(),
        "days": len(values),
        "block_days": block_days,
        "simulations": simulations,
        "seed": seed,
        "policy": "paired moving consecutive-day blocks; experimental uncertainty estimate",
    }
