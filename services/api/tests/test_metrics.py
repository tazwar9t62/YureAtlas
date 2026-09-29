import numpy as np
import pytest

from yureatlas.metrics import aggregate, brier_skill, paired_block_interval, score


def test_exact_scores_and_undefined_metrics():
    result = score([.8,.2,.1,.7], [1,0,1,0], threshold=.5)
    assert result["brier"] == pytest.approx(.345)
    assert result["log_loss"] == pytest.approx(-np.log([.8,.8,.1,.3]).mean())
    assert (result["tp"],result["fp"],result["tn"],result["fn"]) == (1,1,1,1)
    assert result["recall"] == result["precision"] == .5
    empty = score([],[], missing=10)
    assert empty["recall"] is None and empty["brier"] is None and empty["missing"] == 10
    assert score([.01],[0])["precision"] is None
    assert score([.01],[0])["recall"] is None
    assert brier_skill(.1,0) is None


def test_misleading_998_percent_accuracy():
    y = np.zeros(1000, dtype=int)
    y[:2] = 1
    p = np.zeros(1000)
    assert (y == 0).mean() == .998
    metrics = score(p,y)
    assert metrics["recall"] == 0 and metrics["fn"] == 2
    assert metrics["precision"] is None
    assert metrics["confident_misses"] == 2


def test_aggregate_uses_denominators_not_daily_percentages():
    first = score([.9], [1])
    second = score([.001]*9, [1]*9)
    combined = aggregate([first, second])
    assert combined["recall"] == .1
    assert combined["n"] == 10
    assert paired_block_interval([.1]*10)["status"] == "insufficient_data"
    assert paired_block_interval([.1]*28)["interval95"] == pytest.approx([.1,.1])
