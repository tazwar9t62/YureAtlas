import io

import joblib
import numpy as np
import pytest
from scipy.integrate import quad
from yureatlas.etas import ETAS, omori_cdf, productivity
from yureatlas.features import features, labels
from yureatlas.grid import StudyGrid
from yureatlas.rate import HistoricalRate


def catalogue():
    return {
        "t": np.array([1.0, 3.0, 6.0, 8.0, 9.0, 10.0, 11.0]),
        "cell": np.array([0, 0, 1, 1, 1, 2, 3]),
        "m": np.array([4.1, 4.3, 4.8, 4.1, 4.2, 5.1, 6.1]),
        "depth": np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0]),
    }


def test_features_exclude_future_and_expanding_baseline_is_asof():
    grid = StudyGrid(west=140, east=141, south=35, north=36)
    events = catalogue()
    actual = features(events, grid, 10, 4, 0)
    altered = {k: v.copy() for k, v in events.items()}
    altered["m"][events["t"] >= 10] = 9.9
    altered["depth"][events["t"] >= 10] = 900
    np.testing.assert_allclose(actual, features(altered, grid, 10, 4, 0), equal_nan=True)
    assert actual[2, 0] == 0  # t=10 belongs to target, not features.
    assert labels(events, len(grid.ids), 10, 4)[2] == 1
    assert labels(events, len(grid.ids), 10, 4)[3] == 0  # t=11 excluded from [10,11).
    # Future events cannot enter a fitted generated rate either.
    first = HistoricalRate.fit(events, grid, 0, 10, 4)
    second = HistoricalRate.fit(altered, grid, 0, 10, 4)
    np.testing.assert_array_equal(first.predict(), second.predict())
    assert np.all(first.predict() > 0)


def test_model_roundtrip_and_rate_conservation():
    grid = StudyGrid(west=140, east=141, south=35, north=36)
    model = HistoricalRate.fit(catalogue(), grid, 0, 10, 4)
    assert model.rates.sum() == pytest.approx(0.5)
    buffer = io.BytesIO()
    joblib.dump(model, buffer)
    buffer.seek(0)
    restored = joblib.load(buffer)
    np.testing.assert_array_equal(restored.predict(), model.predict())
    assert np.isfinite(restored.predict()).all()
    assert np.all((restored.predict() >= 0) & (restored.predict() <= 1))


def test_etas_normalization_and_subcritical_magnitude_mean():
    c, p = 0.05, 1.4
    integral = quad(lambda t: (p - 1) / c * (1 + t / c) ** (-p), 0, np.inf)[0]
    assert integral == pytest.approx(1, abs=1e-6)
    assert omori_cdf(0, c, p) == 0
    beta, alpha, n = 2.0, 0.8, 0.6
    mean = quad(lambda x: productivity(x, 0, n, alpha, beta) * beta * np.exp(-beta * x), 0, 100)[0]
    assert mean == pytest.approx(n)
    assert n < 1 and alpha < beta


def test_etas_zero_branching_matches_background_poisson_limit():
    grid = StudyGrid(west=140, east=141, south=35, north=36)
    background = grid.areas / grid.areas.sum()
    model = ETAS(
        mu=2,
        branching=0,
        alpha=0.5,
        beta=2,
        c=0.05,
        p=1.5,
        sigma_km=50,
        m0=4,
        background=background,
        simulations=3000,
    )
    events = {"t": np.array([]), "cell": np.array([], dtype=int), "m": np.array([]), "depth": np.array([])}
    probability, expected = model.predict(events, grid, 10)
    analytic = -np.expm1(-2 * background)
    assert np.max(np.abs(probability - analytic)) < 0.035
    assert expected.sum() == pytest.approx(2, abs=0.12)
    matrix = ETAS.spatial(grid, [0, len(grid.ids) - 1], 50)
    np.testing.assert_allclose(matrix.sum(axis=1), 1)


def test_etas_insufficient_fit_is_unavailable_not_fake():
    grid = StudyGrid(west=140, east=141, south=35, north=36)
    with pytest.raises(ValueError, match="40–1800"):
        ETAS.fit(catalogue(), grid, 0, 12, 4)
