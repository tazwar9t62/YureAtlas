"""Independent branching-process expectations and deterministic simulation paths.

These bounded synthetic checks establish software/mathematical behavior, not
earthquake forecasting skill. The mean oracle uses interval bounds on successive
convolutions of exact Omori bin masses, independently from the simulator.
"""

from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.signal import fftconvolve
from yureatlas.etas import ETAS


def one_cell():
    return SimpleNamespace(ids=["cell"], lonlat=np.array([[140.0, 35.0]]), areas=np.array([1.0]))


def catalogue(times=(), magnitudes=None):
    return {
        "t": np.asarray(times, dtype=float),
        "cell": np.zeros(len(times), dtype=int),
        "m": np.full(len(times), 4.0) if magnitudes is None else np.asarray(magnitudes, dtype=float),
        "depth": np.zeros(len(times)),
    }


def model(**changes):
    return ETAS(
        **{
            "mu": 0,
            "branching": 0.5,
            "alpha": 0,
            "beta": 2,
            "c": 0.02,
            "p": 2,
            "sigma_km": 50,
            "m0": 4,
            "background": np.array([1.0]),
            **changes,
        }
    )


def descendant_mean_bounds(bins, generations=32):
    """Bound births by day1 from a parent aged .02d, n=.5,c=.02,p=2.

    Each generation-g sum of g lag-bin indices j lies in
    [j/bins, (j+g)/bins). Thus j+g<=bins guarantees a target birth,
    while j<bins only permits one. Summing these gives lower/upper
    bounds without copying the simulator's inverse CDF or event queue.
    The remaining generations contribute at most lambda*n**g/(1-n).
    """
    edges = np.arange(bins + 1) / bins
    # Integrals of n*c/(c+t)**2; first-generation parent is already .02d old.
    direct = 0.01 * (1 / (0.04 + edges[:-1]) - 1 / (0.04 + edges[1:]))
    offspring = 0.01 * (1 / (0.02 + edges[:-1]) - 1 / (0.02 + edges[1:]))
    mass = direct
    lower = upper = 0.0
    for generation in range(1, generations + 1):
        lower += mass[: max(0, bins - generation + 1)].sum()
        upper += mass.sum()
        # Truncation beyond day1 is safe: all subsequent lags are nonnegative.
        mass = np.maximum(fftconvolve(mass, offspring)[:bins], 0)
    direct_mean = 25 / 104
    tail_bound = direct_mean * 0.5**generations / (1 - 0.5)
    return lower, upper + tail_bound


def test_nonzero_triggering_matches_independent_hit_and_multigeneration_mean():
    coarse = descendant_mean_bounds(4096)
    fine = descendant_mean_bounds(8192)
    assert coarse[0] <= fine[0] + 1e-11 < fine[1] <= coarse[1] + 1e-11
    assert fine[1] - fine[0] < 0.002
    fitted = model(simulations=10_000)
    probability, mean = fitted.predict(catalogue([-0.02]), one_cell(), 0)
    # With no immigrants or unobserved gap, any descendant requires at least
    # one direct child in the target. Direct children are Poisson(lambda).
    direct_mean = 25 / 104
    expected_hit = -np.expm1(-direct_mean)
    hit_se = np.sqrt(expected_hit * (1 - expected_hit) / fitted.simulations)
    smoothing_bias_bound = 0.5 / (fitted.simulations + 1)
    assert abs(probability[0] - expected_hit) <= 6 * hit_se + smoothing_bias_bound
    # The time-truncated count is a compound Poisson sum of truncated trees.
    # A full Poisson(n) tree has second moment 1/(1-n)^3. This bounds the
    # variance without treating correlated descendants as independent events.
    count_se_bound = np.sqrt(direct_mean / (1 - 0.5) ** 3 / fitted.simulations)
    tolerance = 6 * count_se_bound
    assert fine[0] - tolerance <= mean[0] <= fine[1] + tolerance
    assert fine[0] - tolerance > direct_mean  # Missing later generations must fail.
    assert abs(probability[0] - (-np.expm1(-mean[0]))) > 0.05


def test_fixed_seed_repeats_nonzero_triggering_bit_for_bit():
    fitted = model(simulations=256)
    first = fitted.predict(catalogue([-0.02]), one_cell(), 0)
    second = fitted.predict(catalogue([-0.02]), one_cell(), 0)
    for a, b in zip(first, second, strict=True):
        np.testing.assert_array_equal(a, b)
    assert first[1][0] > 0


class ScriptedRNG:
    """Script only random draws; exercise the real ETAS event queue and kernels."""

    def __init__(self, poisson, uniforms=()):
        self.draws = deque(poisson)
        self.uniforms = deque(uniforms)
        self.means = []

    def poisson(self, mean):
        self.means.append(float(mean))
        return self.draws.popleft()

    def uniform(self, low, high, size):
        if not size:
            return np.empty(0)
        value = self.uniforms.popleft() if self.uniforms else (low + high) / 2
        assert low <= value < high
        return np.full(size, value)

    def choice(self, cells, size, p):
        assert np.sum(p) == pytest.approx(1)
        return np.zeros(size, dtype=int)

    def exponential(self, scale, size):
        assert scale > 0
        return np.zeros(size)

    def exhausted(self):
        assert not self.draws and not self.uniforms


def test_unknown_gap_child_spawns_target_second_generation(monkeypatch):
    # Parent -.125 -> child .125 (unknown gap) -> grandchild .375 (target).
    # The gap child must enter the queue but must not itself count as an outcome.
    rng = ScriptedRNG([0, 1, 1, 0], [2 / 3, 2 / 3])
    monkeypatch.setattr(np.random, "default_rng", lambda seed: rng)
    fitted = model(branching=0.6, c=0.125, simulations=1)
    probability, mean = fitted.predict(catalogue([-0.125]), one_cell(), 0, horizon_start=0.25)
    np.testing.assert_allclose(rng.means, [0, 0.25, 0.54, 0.525], rtol=1e-14)
    np.testing.assert_array_equal(mean, [1])
    np.testing.assert_array_equal(probability, [0.75])
    rng.exhausted()


def test_parent_history_is_half_open_and_magnitude_filtered(monkeypatch):
    rng = ScriptedRNG([0, 0, 0])
    monkeypatch.setattr(np.random, "default_rng", lambda seed: rng)
    events = catalogue(
        [np.nextafter(-90.0, -np.inf), -90, -0.02, 0, 0.5, -1],
        [4, 4, 4, 4, 4, 3.9],
    )
    probability, mean = model(simulations=1).predict(events, one_cell(), 0)
    # Only the exact lower history boundary and the recent qualifying parent.
    np.testing.assert_allclose(rng.means, [0, 0.01 * (1 / 90.02 - 1 / 91.02), 25 / 104])
    np.testing.assert_array_equal(mean, [0])
    np.testing.assert_array_equal(probability, [0.25])
    rng.exhausted()


@pytest.mark.parametrize("hits", [0, 1, 4])
def test_jeffreys_smoothing_preserves_raw_mean_and_finite_resolution(monkeypatch, hits):
    # One immigrant and no children in each chosen hit simulation.
    draws = [draw for simulation in range(4) for draw in ([1, 0] if simulation < hits else [0])]
    rng = ScriptedRNG(draws)
    monkeypatch.setattr(np.random, "default_rng", lambda seed: rng)
    probability, mean = model(mu=1, branching=0, simulations=4).predict(catalogue(), one_cell(), 0)
    assert probability[0] == (hits + 0.5) / 5
    assert mean[0] == hits / 4
    assert (probability[0] - 0.1) / 0.2 == pytest.approx(hits)
    rng.exhausted()


def test_default_simulation_floor_is_explicit_even_with_no_possible_events():
    fitted = model(branching=0)
    assert fitted.simulations == 512
    probability, mean = fitted.predict(catalogue(), one_cell(), 0)
    assert probability[0] == 0.5 / 513
    assert probability[0] * 100 == pytest.approx(0.09746588694)
    assert mean[0] == 0  # Smoothing is not an invented expected event count.
