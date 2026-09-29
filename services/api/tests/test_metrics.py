import numpy as np
import pytest
from yureatlas.metrics import aggregate, brier_skill, paired_block_interval, score


def test_exact_scores_and_undefined_metrics():
    result = score([0.8, 0.2, 0.1, 0.7], [1, 0, 1, 0], threshold=0.5)
    assert result["brier"] == pytest.approx(0.345)
    assert result["log_loss"] == pytest.approx(-np.log([0.8, 0.8, 0.1, 0.3]).mean())
    assert (result["tp"], result["fp"], result["tn"], result["fn"]) == (1, 1, 1, 1)
    assert result["recall"] == result["precision"] == 0.5
    empty = score([], [], missing=10)
    assert empty["recall"] is None and empty["brier"] is None and empty["missing"] == 10
    assert score([0.01], [0])["precision"] is None
    assert score([0.01], [0])["recall"] is None
    assert brier_skill(0.1, 0) is None


def test_misleading_998_percent_accuracy():
    y = np.zeros(1000, dtype=int)
    y[:2] = 1
    p = np.zeros(1000)
    assert (y == 0).mean() == 0.998
    metrics = score(p, y)
    assert metrics["recall"] == 0 and metrics["fn"] == 2
    assert metrics["precision"] is None
    assert metrics["confident_misses"] == 2


def test_aggregate_uses_denominators_not_daily_percentages():
    first = score([0.9], [1])
    second = score([0.001] * 9, [1] * 9)
    combined = aggregate([first, second])
    assert combined["recall"] == 0.1
    assert combined["n"] == 10
    assert paired_block_interval([0.1] * 10)["status"] == "insufficient_data"
    assert paired_block_interval([0.1] * 28)["interval95"] == pytest.approx([0.1, 0.1])
