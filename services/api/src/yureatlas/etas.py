"""Fitted discrete-space ETAS research model, Gaussian spatial kernel (gamma=0).

Continuous time, Omori offspring lags, exponential magnitude marks and multi-
generation simulation. Finite-domain normalization is a declared approximation.
See docs/model-notes.md for mathematical checks and external validation limits.
"""

from dataclasses import dataclass, field
from numbers import Integral, Real

import numpy as np
from scipy.optimize import minimize
from sklearn.metrics.pairwise import haversine_distances

MAX_FIT_EVENTS = 1800
MAX_GRID_CELLS = 10_000
MAX_SIMULATION_EVENTS = 20_000
MAX_SIMULATIONS = 10_000
MAX_GAP_DAYS = 1
RESOURCE_ERROR = "ETAS simulation resource limit reached; no partial probabilities published"


def _scalar(name, value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        # Model-input failures share ValueError so training archives unavailable status.
        raise ValueError(f"ETAS {name} must be a finite number")  # noqa: TRY004
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"ETAS {name} must be a finite number") from exc
    if not np.isfinite(value):
        raise ValueError(f"ETAS {name} must be a finite number")
    return value


def _parameters(mu, branching, alpha, beta, c, p, sigma_km, m0):
    values = {
        name: _scalar(name, value)
        for name, value in zip(
            ("mu", "branching", "alpha", "beta", "c", "p", "sigma_km", "m0"),
            (mu, branching, alpha, beta, c, p, sigma_km, m0),
        )
    }
    if not (
        values["mu"] >= 0
        and 0 <= values["branching"] < 1
        and 0 <= values["alpha"] < values["beta"]
        and values["c"] > 0
        and values["p"] > 1
        and values["sigma_km"] > 0
    ):
        raise ValueError("ETAS requires finite positive scales and subcritical branching")


def _numeric_array(name, values):
    array = np.asarray(values)
    if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(f"ETAS {name} must contain finite numeric values")
    return array


def _grid_arrays(grid):
    size = len(grid.ids)
    if not 1 <= size <= MAX_GRID_CELLS or len(set(grid.ids)) != size:
        raise ValueError(f"ETAS grid requires 1–{MAX_GRID_CELLS} unique cells")
    areas = _numeric_array("grid areas", grid.areas)
    coords = _numeric_array("grid coordinates", grid.lonlat)
    if (
        areas.shape != (size,)
        or coords.shape != (size, 2)
        or (areas <= 0).any()
        or not np.isfinite(areas.sum())
        or (np.abs(coords[:, 0]) > 180).any()
        or (np.abs(coords[:, 1]) > 90).any()
    ):
        raise ValueError("ETAS grid areas/coordinates must match its cells and have valid bounds")
    return areas.astype(float), np.radians(coords[:, ::-1])


def _events(events, size):
    if not isinstance(events, dict) or not {"t", "cell", "m"} <= events.keys():
        raise ValueError("ETAS events require t, cell, and m arrays")
    arrays = {name: _numeric_array(f"event {name}", events[name]) for name in ("t", "cell", "m")}
    if any(value.ndim != 1 for value in arrays.values()) or len({len(v) for v in arrays.values()}) != 1:
        raise ValueError("ETAS event arrays must be one-dimensional and equal length")
    cells = arrays["cell"]
    if cells.size and (cells.dtype.kind not in "iu" or (cells < 0).any() or (cells >= size).any()):
        raise ValueError("ETAS event cells must be integer indices within the grid")
    return arrays["t"].astype(float), cells.astype(int), arrays["m"].astype(float)


def _window(start, end):
    start, end = _scalar("window start", start), _scalar("window end", end)
    duration = end - start
    if not np.isfinite(duration) or duration <= 0:
        raise ValueError("ETAS fit duration must be finite and positive")
    return start, end, duration


def _spatial(distances, areas, sigma):
    kernel = np.exp(-0.5 * (distances / sigma) ** 2) * areas[None, :]
    result = kernel / kernel.sum(axis=1, keepdims=True)
    if not np.isfinite(result).all():
        raise ValueError("ETAS spatial kernel is numerically invalid")
    return result


def omori_cdf(lag, c, p):
    return 1 - (1 + np.maximum(lag, 0) / c) ** (1 - p)


def productivity(mag, m0, n, alpha, beta):
    # E[exp(alpha*(m-m0))] = beta/(beta-alpha). Parameter n is
    # the unconditional offspring mean, constrained strictly below one.
    if n == 0:
        return np.zeros_like(np.asarray(mag), dtype=float)
    return n * (beta - alpha) / beta * np.exp(alpha * (np.asarray(mag) - m0))


@dataclass
class ETASLikelihood:
    """Prepared half-open fitting window; geometry stays outside optimization.

    Components use the same empty-initial-history, finite-domain likelihood as
    fit. Intensities follow selected input order; equal-time events cannot be
    parents of one another. Spatial rows follow sorted ``origin_cells``.
    """

    times: np.ndarray
    cells: np.ndarray
    magnitudes: np.ndarray
    origin_cells: np.ndarray
    inverse: np.ndarray
    distances: np.ndarray
    areas: np.ndarray
    background: np.ndarray
    lag: np.ndarray
    duration: float
    end: float
    m0: float

    def components(self, *, mu, branching, alpha, beta, c, p, sigma_km):
        _parameters(mu, branching, alpha, beta, c, p, sigma_km, self.m0)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                k = productivity(self.magnitudes, self.m0, branching, alpha, beta)
                spatial = _spatial(self.distances, self.areas, sigma_km)
                # Rows are children; columns are strictly preceding parents.
                pair = spatial[self.inverse][:, self.cells].T
                temporal = (p - 1) / c * (1 + np.maximum(self.lag, 0) / c) ** (-p) * (self.lag > 0)
                intensity = mu * self.background[self.cells] + (temporal * pair * k[None, :]).sum(axis=1)
                background_integral = mu * self.duration
                triggered_integral = float(np.sum(k * omori_cdf(self.end - self.times, c, p)))
                integral = background_integral + triggered_integral
                if not np.isfinite(intensity).all() or (intensity <= 0).any():
                    raise ValueError("ETAS likelihood requires positive finite event intensities")
                nll = float(integral - np.log(intensity).sum())
                if not np.isfinite(integral) or not np.isfinite(nll):
                    raise ValueError("ETAS likelihood is numerically invalid")
        except FloatingPointError as exc:
            raise ValueError("ETAS likelihood numerical overflow; fit unavailable") from exc
        return {
            "intensities": intensity,
            "spatial": spatial,
            "origin_cells": self.origin_cells,
            "background_integral": float(background_integral),
            "triggered_integral": triggered_integral,
            "integral": float(integral),
            "log_likelihood": -nll,
            "negative_log_likelihood": nll,
        }


def prepare_likelihood(events, grid, start, end, m0):
    """Prepare the actual fitting objective, also usable for small math fixtures."""
    start, end, duration = _window(start, end)
    m0 = _scalar("m0", m0)
    areas, coords = _grid_arrays(grid)
    t, cells, mag = _events(events, len(grid.ids))
    mask = (t >= start) & (t < end) & (mag >= m0)
    t, cells, mag = t[mask], cells[mask], mag[mask]
    if len(t) > MAX_FIT_EVENTS:
        raise ValueError(f"ETAS fit resource limit is {MAX_FIT_EVENTS} target-threshold events")
    unique, inverse = np.unique(cells, return_inverse=True)
    distances = (
        haversine_distances(coords[unique], coords) * 6371.0088 if len(unique) else np.empty((0, len(areas)))
    )
    with np.errstate(over="raise", invalid="raise"):
        try:
            lag = t[:, None] - t[None, :]
        except FloatingPointError as exc:
            raise ValueError("ETAS event time differences must be finite") from exc
    return ETASLikelihood(
        t, cells, mag, unique, inverse, distances, areas, areas / areas.sum(), lag, duration, end, m0
    )


@dataclass
class ETAS:
    mu: float
    branching: float
    alpha: float
    beta: float
    c: float
    p: float
    sigma_km: float
    m0: float
    background: np.ndarray
    diagnostics: dict = field(default_factory=dict)
    algorithm: str = "etas_discrete_space"
    simulations: int = 512
    seed: int = 2026

    def __post_init__(self):
        _parameters(self.mu, self.branching, self.alpha, self.beta, self.c, self.p, self.sigma_km, self.m0)
        if (
            isinstance(self.simulations, (bool, np.bool_))
            or not isinstance(self.simulations, Integral)
            or not 1 <= self.simulations <= MAX_SIMULATIONS
        ):
            raise ValueError(f"ETAS simulations must be an integer between 1 and {MAX_SIMULATIONS}")
        if isinstance(self.seed, (bool, np.bool_)) or not isinstance(self.seed, Integral) or self.seed < 0:
            raise ValueError("ETAS seed must be a nonnegative integer")
        background = _numeric_array("background", self.background)
        if (
            background.ndim != 1
            or not 1 <= len(background) <= MAX_GRID_CELLS
            or (background < 0).any()
            or not np.isclose(background.sum(), 1, rtol=1e-8, atol=1e-12)
        ):
            raise ValueError("ETAS background must be a normalized spatial distribution")
        self.background = background.astype(float)

    @staticmethod
    def spatial(grid, origins, sigma):
        sigma = _scalar("sigma_km", sigma)
        if sigma <= 0:
            raise ValueError("ETAS sigma_km must be positive")
        areas, coords = _grid_arrays(grid)
        origins = np.asarray(origins)
        if origins.ndim != 1 or (
            origins.size
            and (origins.dtype.kind not in "iu" or (origins < 0).any() or (origins >= len(areas)).any())
        ):
            raise ValueError("ETAS spatial origins must be integer indices within the grid")
        if not origins.size:
            return np.empty((0, len(areas)))
        if len(origins) > MAX_GRID_CELLS or len(origins) * len(areas) > MAX_FIT_EVENTS * MAX_GRID_CELLS:
            raise ValueError("ETAS spatial origin resource limit reached")
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                distances = haversine_distances(coords[origins.astype(int)], coords) * 6371.0088
                return _spatial(distances, areas, sigma)
        except FloatingPointError as exc:
            raise ValueError("ETAS spatial kernel numerical overflow") from exc

    @classmethod
    def fit(cls, events, grid, start, end, m0, progress=lambda p: None):
        prepared = prepare_likelihood(events, grid, start, end, m0)
        if len(prepared.times) < 40:
            raise ValueError(
                f"ETAS needs 40–1800 target-threshold events in the declared fit window; received {len(prepared.times)}"
            )
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                beta = 1 / max(float((prepared.magnitudes - m0).mean()), 0.1)
                rate = len(prepared.times) / prepared.duration
            if not np.isfinite(rate * 3):
                raise ValueError("ETAS fit rate must be finite")
        except FloatingPointError as exc:
            raise ValueError("ETAS fitting statistics numerical overflow") from exc

        def loss(theta):
            mu, n, ratio, c, p, sigma = theta
            return prepared.components(
                mu=mu, branching=n, alpha=ratio * beta, beta=beta, c=c, p=p, sigma_km=sigma
            )["negative_log_likelihood"]

        bounds = [(1e-5, max(rate * 3, 1)), (0.001, 0.85), (0.01, 0.8), (0.005, 1), (1.05, 2.5), (25, 250)]
        opt = minimize(
            loss,
            [rate * 0.6, 0.35, 0.35, 0.05, 1.4, 70],
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": 100, "ftol": 1e-8},
        )
        progress(1)
        theta = np.asarray(opt.x)
        if (
            not opt.success
            or not np.isfinite(opt.fun)
            or theta.shape != (6,)
            or not np.isfinite(theta).all()
            or any(not lower <= value <= upper for value, (lower, upper) in zip(theta, bounds))
        ):
            raise ValueError(f"ETAS optimizer unavailable: {opt.message}; iterations={opt.nit}")
        # Do not trust a finite reported objective for invalid final parameters.
        objective = loss(theta)
        if not np.isclose(objective, opt.fun, rtol=1e-7, atol=1e-8):
            raise ValueError("ETAS optimizer objective does not match its final parameters")
        mu, n, ratio, c, p, sigma = theta
        return cls(
            float(mu),
            float(n),
            float(ratio * beta),
            beta,
            float(c),
            float(p),
            float(sigma),
            m0,
            prepared.background,
            {
                "converged": bool(opt.success),
                "negative_log_likelihood": objective,
                "iterations": int(opt.nit),
                "fit_events": len(prepared.times),
                "fit_days": prepared.duration,
                "branching_ratio": float(n),
                "boundary": "finite domain normalized offspring; no outside triggering",
                "coordinate_units": "haversine km at cell centroids",
                "history": "empty at fit start; prediction parents truncated at 90 days",
                "external_validation": "pending",
                "spatial_approximation": "area-weighted Gaussian masses at retained-grid cell centroids, gamma fixed 0",
                "magnitude_assumption": "stationary exponential excess magnitudes above target threshold",
                "parameters": {
                    "mu": float(mu),
                    "n": float(n),
                    "alpha": float(ratio * beta),
                    "beta": beta,
                    "c_days": float(c),
                    "p": float(p),
                    "sigma_km": float(sigma),
                },
            },
        )

    def predict(self, events, grid, cutoff, horizon_start=None, progress=lambda p: None):
        # joblib loading bypasses __post_init__; validate archived state on use too.
        self.__post_init__()
        areas, coords = _grid_arrays(grid)
        if len(self.background) != len(areas):
            raise ValueError("ETAS background size must match the prediction grid")
        cutoff = _scalar("cutoff", cutoff)
        start = cutoff if horizon_start is None else _scalar("horizon start", horizon_start)
        horizon = start + 1
        gap = start - cutoff
        if not 0 <= gap <= MAX_GAP_DAYS or not np.isfinite(horizon) or horizon - start != 1:
            raise ValueError(f"ETAS horizon must start between cutoff and cutoff + {MAX_GAP_DAYS} day")
        t, cells, mag = _events(events, len(areas))
        mask = (t < cutoff) & (t >= cutoff - 90) & (mag >= self.m0)
        if np.count_nonzero(mask) > MAX_SIMULATION_EVENTS:
            raise ValueError(RESOURCE_ERROR)
        parents = list(zip(t[mask], cells[mask], mag[mask]))
        rng = np.random.default_rng(self.seed)
        counts = np.zeros(len(areas))
        hits = np.zeros(len(areas))
        spatial_cache = {}

        def spatial(cell):
            if cell not in spatial_cache:
                distances = haversine_distances(coords[[cell]], coords) * 6371.0088
                spatial_cache[cell] = _spatial(distances, areas, self.sigma_km)[0]
            return spatial_cache[cell]

        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                background_mean = self.mu * (horizon - cutoff)
                if not np.isfinite(background_mean):
                    raise ValueError("ETAS background intensity must be finite")
                for simulation in range(self.simulations):
                    background_n = rng.poisson(background_mean)
                    # Includes observed parents, before allocating immigrant arrays.
                    if len(parents) + background_n > MAX_SIMULATION_EVENTS:
                        raise ValueError(RESOURCE_ERROR)
                    immigrant_times = rng.uniform(cutoff, horizon, background_n)
                    immigrant_cells = rng.choice(len(areas), background_n, p=self.background)
                    immigrant_magnitudes = self.m0 + rng.exponential(1 / self.beta, background_n)
                    if not np.isfinite(immigrant_times).all() or not np.isfinite(immigrant_magnitudes).all():
                        raise ValueError("ETAS generated events must be finite")
                    generated = [
                        (float(t), int(cell), float(m))
                        for t, cell, m in zip(
                            immigrant_times,
                            immigrant_cells,
                            immigrant_magnitudes,
                        )
                    ]
                    queue = parents + generated
                    cursor = 0
                    while cursor < len(queue):
                        parent_time, cell, magnitude = queue[cursor]
                        cursor += 1
                        lo = max(0, cutoff - parent_time)
                        hi = horizon - parent_time
                        f0, f1 = omori_cdf(lo, self.c, self.p), omori_cdf(hi, self.c, self.p)
                        expected = productivity(magnitude, self.m0, self.branching, self.alpha, self.beta) * (
                            f1 - f0
                        )
                        if not np.isfinite(expected) or expected < 0:
                            raise ValueError("ETAS offspring intensity must be finite and nonnegative")
                        n = rng.poisson(expected)
                        if len(queue) + n > MAX_SIMULATION_EVENTS:
                            raise ValueError(RESOURCE_ERROR)
                        if n:
                            lags = self.c * ((1 - rng.uniform(f0, f1, n)) ** (1 / (1 - self.p)) - 1)
                            child_times = parent_time + lags
                            child_cells = rng.choice(len(areas), n, p=spatial(int(cell)))
                            child_magnitudes = self.m0 + rng.exponential(1 / self.beta, n)
                            if not np.isfinite(child_times).all() or not np.isfinite(child_magnitudes).all():
                                raise ValueError("ETAS generated events must be finite")
                            children = list(
                                zip(
                                    child_times,
                                    child_cells,
                                    child_magnitudes,
                                )
                            )
                            generated.extend(children)
                            queue.extend(children)
                    samples = [int(c) for t, c, m in generated if start <= t < horizon]
                    histogram = np.bincount(samples, minlength=len(areas))
                    counts += histogram
                    hits += histogram > 0
                    if simulation % 32 == 0:
                        progress(simulation / self.simulations)
        except FloatingPointError as exc:
            raise ValueError(
                "ETAS simulation numerical overflow; no partial probabilities published"
            ) from exc
        # Raw Monte Carlo hit frequencies, not a Poisson ETAS conversion.
        # Jeffreys smoothing includes finite-simulation uncertainty explicitly.
        probabilities = (hits + 0.5) / (self.simulations + 1)
        return probabilities, counts / self.simulations
