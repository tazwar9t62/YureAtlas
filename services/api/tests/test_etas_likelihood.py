"""Independent discrete-space likelihood oracles, not external ETAS validation.

Ogata (1998), Ann. Inst. Statist. Math. 50, pp. 381/384/385, equations
2.1/2.5/2.6, supplies the conditional-intensity and sum(log intensity)-integral
form: https://www.ism.ac.jp/editsec/aism/pdf/050_2_0379.pdf.
The area-normalized two-cell Gaussian below is this project's declared spatial
approximation, not the paper's continuous fitted spatial/background model.
"""

import math
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.integrate import quad
from yureatlas import etas

SIGMA = 50.0
DISTANCE = SIGMA * math.sqrt(2 * math.log(2))
PARAMETERS = {
    "mu": 2.0,
    "branching": 0.4,
    "alpha": math.log(2),
    "beta": 2 * math.log(2),
    "c": 1.0,
    "p": 2.0,
    "sigma_km": SIGMA,
}


def two_cells():
    # At the equator, great-circle separation is exactly R*delta-longitude.
    return SimpleNamespace(
        lonlat=np.array([[0.0, 0.0], [math.degrees(DISTANCE / 6371.0088), 0.0]]),
        areas=np.array([1.0, 3.0]),
        ids=["west", "east"],
    )


def three_events():
    return {
        "t": np.array([0.25, 0.75, 1.5]),
        "cell": np.array([0, 1, 0]),
        "m": np.array([4.0, 5.0, 6.0]),
    }


def independent_components(events, start, end, parameters):
    """Scalar point-process definition using known geometry, not production helpers."""
    rows = [
        (float(time), int(cell), float(magnitude))
        for time, cell, magnitude in zip(events["t"], events["cell"], events["m"], strict=True)
        if start <= time < end and magnitude >= 4
    ]
    mu, n, alpha, beta, c, p, sigma = (
        parameters[name] for name in ("mu", "branching", "alpha", "beta", "c", "p", "sigma_km")
    )
    cross = math.exp(-(DISTANCE**2) / (2 * sigma**2))
    spatial = [[1 / (1 + 3 * cross), 3 * cross / (1 + 3 * cross)], [cross / (cross + 3), 3 / (cross + 3)]]
    marks = [n * (1 - alpha / beta) * math.exp(alpha * (magnitude - 4)) for _, _, magnitude in rows]
    intensities = []
    for child_time, child_cell, _ in rows:
        intensity = mu * (0.25 if child_cell == 0 else 0.75)
        for (parent_time, parent_cell, _), mark in zip(rows, marks, strict=True):
            if parent_time < child_time:
                intensity += (
                    mark
                    * (p - 1)
                    * c ** (p - 1)
                    / (child_time - parent_time + c) ** p
                    * spatial[parent_cell][child_cell]
                )
        intensities.append(intensity)
    triggered = sum(
        mark * (1 - (c / (end - time + c)) ** (p - 1)) for (time, _, _), mark in zip(rows, marks, strict=True)
    )
    integral = mu * (end - start) + triggered
    return np.array(intensities), integral, sum(math.log(value) for value in intensities) - integral


def test_area_weighted_spatial_masses_and_exponential_mark_normalization():
    grid = two_cells()
    expected = np.array([[2 / 5, 3 / 5], [1 / 7, 6 / 7]])
    np.testing.assert_allclose(etas.ETAS.spatial(grid, [0, 1], SIGMA), expected, rtol=1e-14, atol=1e-15)
    np.testing.assert_allclose(etas.ETAS.spatial(grid, [1, 0, 1], SIGMA), expected[[1, 0, 1]], rtol=1e-14)
    np.testing.assert_allclose(expected.sum(axis=1), 1, rtol=0, atol=1e-15)
    assert expected[0, 1] != expected[1, 0]  # Parent/child transpose cannot pass this fixture.
    np.testing.assert_allclose(
        etas.productivity(np.array([4, 5, 6]), 4, 0.4, math.log(2), 2 * math.log(2)),
        [0.2, 0.4, 0.8],
        rtol=1e-14,
    )
    # Independently integrate productivity against the exponential mark density.
    # The finite endpoint has exactly computable missing mass 0.4*2**(-40).
    integrated, error = quad(
        lambda excess: (
            float(etas.productivity(4 + excess, 4, 0.4, math.log(2), 2 * math.log(2)))
            * 2
            * math.log(2)
            * math.exp(-2 * math.log(2) * excess)
        ),
        0,
        40,
        epsabs=1e-12,
        epsrel=1e-12,
    )
    assert integrated == pytest.approx(0.4 * (1 - 2**-40), abs=2e-12)
    assert error < 1e-11


def test_exact_three_event_likelihood_and_independent_time_quadrature():
    events = three_events()
    result = etas.prepare_likelihood(events, two_cells(), 0, 2, 4).components(**PARAMETERS)
    expected_intensities = np.array(
        [
            float(Fraction(1, 2)),
            float(Fraction(3, 2) + Fraction(1, 5) * Fraction(4, 9) * Fraction(3, 5)),
            float(
                Fraction(1, 2)
                + Fraction(1, 5) * Fraction(16, 81) * Fraction(2, 5)
                + Fraction(2, 5) * Fraction(16, 49) * Fraction(1, 7)
            ),
        ]
    )
    expected_triggered = float(
        Fraction(1, 5) * Fraction(7, 11) + Fraction(2, 5) * Fraction(5, 9) + Fraction(4, 5) * Fraction(1, 3)
    )
    expected_integral = 4 + expected_triggered
    np.testing.assert_allclose(result["intensities"], expected_intensities, rtol=1e-14)
    np.testing.assert_array_equal(result["origin_cells"], [0, 1])
    np.testing.assert_allclose(result["spatial"], [[2 / 5, 3 / 5], [1 / 7, 6 / 7]], rtol=1e-14)
    assert result["background_integral"] == 4
    assert result["triggered_integral"] == pytest.approx(expected_triggered, abs=1e-14)
    assert result["integral"] == pytest.approx(expected_integral, abs=1e-14)
    expected_log_likelihood = sum(math.log(value) for value in expected_intensities) - expected_integral
    assert result["log_likelihood"] == pytest.approx(expected_log_likelihood, abs=1e-13)
    assert result["negative_log_likelihood"] == pytest.approx(-expected_log_likelihood, abs=1e-13)
    numeric_integral, error = quad(
        lambda time: (
            2
            + sum(
                mark / (1 + time - parent) ** 2
                for parent, mark in zip([0.25, 0.75, 1.5], [0.2, 0.4, 0.8], strict=True)
                if parent < time
            )
        ),
        0,
        2,
        points=[0.25, 0.75, 1.5],
        epsabs=1e-12,
        epsrel=1e-12,
    )
    assert numeric_integral == pytest.approx(expected_integral, abs=1e-12)
    assert error < 1e-11


def test_strict_parent_times_half_open_fit_window_and_permutation_invariance():
    events = {
        "t": np.array([-0.1, 0.0, 0.0, 0.75, 1.5, 2.0, 0.5]),
        "cell": np.array([0, 0, 1, 1, 0, 0, 0]),
        "m": np.array([8.0, 4.0, 5.0, 5.0, 6.0, 8.0, 3.9]),
    }
    expected_intensities, expected_integral, expected_log = independent_components(events, 0, 2, PARAMETERS)
    original = etas.prepare_likelihood(events, two_cells(), 0, 2, 4).components(**PARAMETERS)
    assert len(original["intensities"]) == 4
    np.testing.assert_allclose(original["intensities"], expected_intensities, rtol=1e-14)
    np.testing.assert_allclose(original["intensities"][:2], [0.5, 1.5], rtol=0, atol=1e-15)
    assert original["integral"] == pytest.approx(expected_integral, abs=1e-13)
    assert original["log_likelihood"] == pytest.approx(expected_log, abs=1e-13)
    permutation = np.array([4, 2, 5, 0, 3, 6, 1])
    shuffled = {name: values[permutation] for name, values in events.items()}
    result = etas.prepare_likelihood(shuffled, two_cells(), 0, 2, 4).components(**PARAMETERS)
    np.testing.assert_allclose(result["intensities"], expected_intensities[[3, 1, 2, 0]], rtol=1e-14)
    assert result["integral"] == pytest.approx(original["integral"], abs=1e-13)
    assert result["log_likelihood"] == pytest.approx(original["log_likelihood"], abs=1e-13)


def test_empty_window_likelihood_is_negative_background_compensator():
    events = {"t": np.array([]), "cell": np.array([], dtype=int), "m": np.array([])}
    result = etas.prepare_likelihood(events, two_cells(), 2, 5, 4).components(**PARAMETERS)
    assert len(result["intensities"]) == 0
    assert result["triggered_integral"] == 0
    assert result["background_integral"] == result["integral"] == 6
    assert result["log_likelihood"] == -6 and result["negative_log_likelihood"] == 6
    with pytest.raises(ValueError, match="intensit"):
        etas.prepare_likelihood(three_events(), two_cells(), 0, 2, 4).components(
            **dict(PARAMETERS, mu=0, branching=0)
        )


def test_fit_optimizer_uses_the_same_normalized_likelihood_and_selected_window(monkeypatch):
    base = three_events()
    events = {
        "t": np.concatenate([base["t"] + block * 2 for block in range(16)] + [np.array([-1, 32, 1])]),
        "cell": np.concatenate([base["cell"] for _ in range(16)] + [np.array([0, 1, 0])]),
        "m": np.concatenate([np.array([4.0, 4.5, 5.0]) for _ in range(16)] + [np.array([9, 9, 3.9])]),
    }
    calls = []
    chosen = np.array([2.0, 0.4, 0.5, 1.0, 2.0, SIGMA])

    def optimizer(objective, initial, **options):
        # This is deliberately a wiring probe, not an optimizer convergence test.
        assert options["method"] == "L-BFGS-B"
        for theta in (chosen, np.array([1.5, 0.6, 0.25, 0.5, 1.5, 70])):
            mu, branching, alpha_over_beta, c, p, sigma = theta
            parameters = {
                "mu": mu,
                "branching": branching,
                "alpha": alpha_over_beta * 2,
                "beta": 2.0,
                "c": c,
                "p": p,
                "sigma_km": sigma,
            }
            expected = independent_components(events, 0, 32, parameters)[2]
            actual = objective(theta)
            assert actual == pytest.approx(-expected, rel=1e-13, abs=1e-12)
            calls.append(actual)
        return SimpleNamespace(
            success=True, x=chosen, fun=calls[0], nit=0, message="Likelihood wiring probe only"
        )

    monkeypatch.setattr(etas, "minimize", optimizer)
    model = etas.ETAS.fit(events, two_cells(), 0, 32, 4)
    assert len(calls) == 2
    assert model.beta == 2 and model.alpha == 1
    assert model.diagnostics["fit_events"] == 48 and model.diagnostics["fit_days"] == 32
    assert model.diagnostics["negative_log_likelihood"] == calls[0]
    np.testing.assert_allclose(model.background, [0.25, 0.75])
    np.testing.assert_allclose(
        [model.mu, model.branching, model.alpha / model.beta, model.c, model.p, model.sigma_km], chosen
    )


def test_bounded_real_optimizer_smoke_has_finite_independently_recomputed_objective():
    index = np.arange(48)
    events = {"t": np.linspace(0.05, 9.95, 48), "cell": index % 2, "m": 4.25 + (index % 5) * 0.15}
    model = etas.ETAS.fit(events, two_cells(), 0, 10, 4)
    parameters = {
        "mu": model.mu,
        "branching": model.branching,
        "alpha": model.alpha,
        "beta": model.beta,
        "c": model.c,
        "p": model.p,
        "sigma_km": model.sigma_km,
    }
    expected = -independent_components(events, 0, 10, parameters)[2]
    assert model.diagnostics["converged"] is True
    assert 0 <= model.diagnostics["iterations"] <= 100
    assert math.isfinite(expected)
    assert model.diagnostics["negative_log_likelihood"] == pytest.approx(expected, rel=1e-12, abs=1e-11)
    initial = {
        "mu": 4.8 * 0.6,
        "branching": 0.35,
        "alpha": 0.35 * model.beta,
        "beta": model.beta,
        "c": 0.05,
        "p": 1.4,
        "sigma_km": 70,
    }
    assert expected <= -independent_components(events, 0, 10, initial)[2] + 1e-8
