"""ETAS rejects malformed/numerically unsafe work before returning predictions."""

import io
from types import SimpleNamespace

import joblib
import numpy as np
import pytest
from yureatlas import etas
from yureatlas.etas import ETAS, prepare_likelihood


def grid():
    return SimpleNamespace(
        ids=["west", "east"], areas=np.array([1.0, 3.0]), lonlat=np.array([[140.0, 35.0], [141.0, 35.0]])
    )


def events(size=0):
    return {"t": np.linspace(1, 8, size), "cell": np.zeros(size, dtype=int), "m": np.full(size, 4.5)}


def model(**changes):
    values = {
        "mu": 1.0,
        "branching": 0.4,
        "alpha": 0.5,
        "beta": 2.0,
        "c": 0.05,
        "p": 1.5,
        "sigma_km": 50.0,
        "m0": 4.0,
        "background": np.array([0.25, 0.75]),
        "simulations": 2,
    }
    values.update(changes)
    return ETAS(**values)


@pytest.mark.parametrize("name", ["mu", "branching", "alpha", "beta", "c", "p", "sigma_km", "m0"])
@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, True, "1"])
def test_every_model_scalar_requires_finite_numeric_value(name, value):
    with pytest.raises(ValueError, match=f"{name} must be a finite number"):
        model(**{name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"mu": -1},
        {"branching": -0.1},
        {"branching": 1},
        {"alpha": -1},
        {"alpha": 2},
        {"beta": 0},
        {"c": 0},
        {"p": 1},
        {"sigma_km": 0},
    ],
)
def test_subcritical_physical_parameter_domain(changes):
    with pytest.raises(ValueError, match="positive scales and subcritical"):
        model(**changes)


@pytest.mark.parametrize("simulations", [0, -1, 1.5, 1.0, True, "2", etas.MAX_SIMULATIONS + 1])
def test_simulations_are_bounded_strict_integers(simulations):
    with pytest.raises(ValueError, match="simulations must be an integer"):
        model(simulations=simulations)


@pytest.mark.parametrize("seed", [-1, True, 1.5, "2026"])
def test_seed_is_a_nonnegative_integer(seed):
    with pytest.raises(ValueError, match="seed must be a nonnegative integer"):
        model(seed=seed)


@pytest.mark.parametrize(
    "background", [[], [[0.25, 0.75]], [0.2, 0.2], [-0.1, 1.1], [np.nan, 1], [False, True]]
)
def test_background_is_finite_vector_probability_mass(background):
    with pytest.raises(ValueError, match="background"):
        model(background=background)


@pytest.mark.parametrize(
    "field,value",
    [
        ("t", [[1.0]]),
        ("m", [4.0, 5.0]),
        ("t", [np.nan]),
        ("m", [np.inf]),
        ("m", ["4.5"]),
        ("m", [True]),
        ("cell", [0.0]),
        ("cell", [-1]),
        ("cell", [2]),
        ("cell", [False]),
    ],
)
@pytest.mark.parametrize("operation", ["fit", "predict"])
def test_event_arrays_validated_before_filtering_or_fitting(field, value, operation, monkeypatch):
    malformed = events(1)
    malformed[field] = np.asarray(value)
    monkeypatch.setattr(etas, "minimize", lambda *a, **k: pytest.fail("optimizer reached invalid events"))
    monkeypatch.setattr(
        etas.np.random, "default_rng", lambda *a, **k: pytest.fail("simulation reached invalid events")
    )
    with pytest.raises(ValueError, match="ETAS event"):
        if operation == "fit":
            ETAS.fit(malformed, grid(), 0, 10, 4)
        else:
            model().predict(malformed, grid(), 10)


def test_missing_event_array_is_explicit_error():
    with pytest.raises(ValueError, match="require t, cell, and m"):
        prepare_likelihood({"t": [], "m": []}, grid(), 0, 10, 4)


@pytest.mark.parametrize(
    "start,end", [(0, 0), (2, 1), (0, np.inf), (np.nan, 10), (-1e308, 1e308), (True, 10)]
)
def test_fitting_window_has_finite_positive_duration(start, end):
    with pytest.raises(ValueError, match="ETAS (fit duration|window)"):
        ETAS.fit(events(40), grid(), start, end, 4)


@pytest.mark.parametrize(
    "change",
    [
        {"areas": np.array([1, 0])},
        {"areas": np.array([1, np.inf])},
        {"areas": np.ones(3)},
        {"lonlat": np.array([[181, 0], [140, 35]])},
        {"lonlat": np.zeros((2, 3))},
        {"ids": ["same", "same"]},
    ],
)
def test_grid_geometry_and_identity_are_validated(change):
    candidate_grid = grid()
    candidate_grid.__dict__.update(change)
    with pytest.raises(ValueError, match="ETAS grid"):
        model().predict(events(), candidate_grid, 10)


def test_grid_background_size_must_match():
    with pytest.raises(ValueError, match="background size must match"):
        model(background=[1]).predict(events(), grid(), 10)


def test_grid_cap_precedes_geometry_materialization():
    class OversizedGrid:
        ids = range(etas.MAX_GRID_CELLS + 1)

        @property
        def areas(self):
            pytest.fail("oversized grid geometry was read")

    with pytest.raises(ValueError, match="unique cells"):
        model().predict(events(), OversizedGrid(), 10)


@pytest.mark.parametrize(
    "cutoff,start", [(10, 9), (10, 11.00001), (np.nan, None), (10, np.inf), (1e308, None), (2**53 + 2, None)]
)
def test_prediction_horizon_is_bounded_and_representable(cutoff, start):
    with pytest.raises(ValueError, match="ETAS (cutoff|horizon)"):
        model().predict(events(), grid(), cutoff, horizon_start=start)


def test_fit_cap_precedes_quadratic_geometry(monkeypatch):
    monkeypatch.setattr(
        etas, "haversine_distances", lambda *a, **k: pytest.fail("oversized fit allocated geometry")
    )
    with pytest.raises(ValueError, match="fit resource limit"):
        ETAS.fit(events(etas.MAX_FIT_EVENTS + 1), grid(), 0, 10, 4)


def test_parent_cap_precedes_rng_and_queue_allocation(monkeypatch):
    monkeypatch.setattr(
        etas.np.random, "default_rng", lambda *a, **k: pytest.fail("RNG reached oversized parent list")
    )
    with pytest.raises(ValueError, match="simulation resource limit"):
        model().predict(events(etas.MAX_SIMULATION_EVENTS + 1), grid(), 10)


class ResourceDraws:
    def __init__(self, draws):
        self.draws = iter(draws)
        self.allocated = []

    def poisson(self, mean):
        return next(self.draws)

    def uniform(self, lo, hi, size):
        self.allocated.append(size)
        if size:
            pytest.fail("allocation happened after a cap-exceeding count")
        return np.empty(0)

    def choice(self, cells, size, p):
        self.allocated.append(size)
        assert size == 0
        return np.empty(0, dtype=int)

    def exponential(self, scale, size):
        self.allocated.append(size)
        assert size == 0
        return np.empty(0)


@pytest.mark.parametrize("draws", [[etas.MAX_SIMULATION_EVENTS], [0, etas.MAX_SIMULATION_EVENTS]])
def test_immigrant_and_descendant_caps_include_existing_parents_before_allocation(monkeypatch, draws):
    rng = ResourceDraws(draws)
    monkeypatch.setattr(etas.np.random, "default_rng", lambda seed: rng)
    with pytest.raises(ValueError, match="no partial probabilities published"):
        model().predict(events(1), grid(), 10)
    assert not any(rng.allocated)


def test_high_magnitude_numerical_overflow_fails_without_partial_prediction():
    high = events(1)
    high["m"][:] = 1e308
    with pytest.raises(ValueError, match="numerical overflow"):
        model().predict(high, grid(), 10)
    # Exactly zero branching has zero productivity even at extreme finite marks.
    probabilities, expected = model(branching=0, mu=0).predict(high, grid(), 10)
    np.testing.assert_array_equal(expected, [0, 0])
    np.testing.assert_array_equal(probabilities, [1 / 6, 1 / 6])


def test_likelihood_overflow_is_unavailable_not_a_finite_optimizer_score():
    high = events(40)
    high["m"][:] = 1e308
    prepared = prepare_likelihood(high, grid(), 0, 10, 4)
    with pytest.raises(ValueError, match="numerical overflow"):
        prepared.components(mu=1, branching=0.4, alpha=1, beta=2, c=0.05, p=1.5, sigma_km=50)


@pytest.mark.parametrize(
    "failure",
    [
        "nonconvergence",
        "objective_nan",
        "parameter_nan",
        "parameter_outside_bounds",
        "parameter_shape",
        "objective_mismatch",
    ],
)
def test_optimizer_failure_or_invalid_final_parameters_never_returns_model(monkeypatch, failure):
    def optimizer(objective, initial, **options):
        theta = np.array(initial, dtype=float)
        score = objective(theta)
        success = True
        if failure == "nonconvergence":
            success = False
        elif failure == "objective_nan":
            score = np.nan
        elif failure == "parameter_nan":
            theta[0] = np.nan
        elif failure == "parameter_outside_bounds":
            theta[1] = 0.99
        elif failure == "parameter_shape":
            theta = theta[:3]
        elif failure == "objective_mismatch":
            score += 10
        return SimpleNamespace(success=success, x=theta, fun=score, message="controlled failure", nit=1)

    monkeypatch.setattr(etas, "minimize", optimizer)
    with pytest.raises(ValueError, match="ETAS optimizer"):
        ETAS.fit(events(40), grid(), 0, 10, 4)


def test_existing_dataclass_archive_roundtrip_and_validation_on_use():
    original = model()
    baseline = original.predict(events(1), grid(), 10)
    buffer = io.BytesIO()
    joblib.dump(original, buffer)
    buffer.seek(0)
    loaded = joblib.load(buffer)
    for before, after in zip(baseline, loaded.predict(events(1), grid(), 10)):
        np.testing.assert_array_equal(before, after)
    # Loading bypasses __post_init__, including for older archived instances.
    loaded.simulations = 0
    with pytest.raises(ValueError, match="simulations must be an integer"):
        loaded.predict(events(1), grid(), 10)
