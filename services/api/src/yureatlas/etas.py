"""Fitted discrete-space ETAS research model, Gaussian spatial kernel (gamma=0).

Continuous time, Omori offspring lags, exponential magnitude marks and multi-
generation simulation. Finite-domain normalization is a declared approximation.
See docs/model-notes.md; independent scientific validation remains pending.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from sklearn.metrics.pairwise import haversine_distances


def omori_cdf(lag, c, p):
    return 1 - (1 + np.maximum(lag, 0) / c) ** (1 - p)


def productivity(mag, m0, n, alpha, beta):
    # E[exp(alpha*(m-m0))] = beta/(beta-alpha). Parameter n is
    # the unconditional offspring mean, constrained strictly below one.
    return n * (beta - alpha) / beta * np.exp(alpha * (np.asarray(mag) - m0))


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
        if not (
            0 <= self.branching < 1
            and 0 <= self.alpha < self.beta
            and self.c > 0
            and self.p > 1
            and self.sigma_km > 0
            and self.mu >= 0
            and self.simulations > 0
        ):
            raise ValueError("ETAS requires finite positive scales and subcritical branching")
        if (
            not np.isfinite(self.background).all()
            or (self.background < 0).any()
            or not np.isclose(self.background.sum(), 1)
        ):
            raise ValueError("ETAS background must be a normalized spatial distribution")

    @staticmethod
    def spatial(grid, origins, sigma):
        coords = np.radians(grid.lonlat[:, ::-1])
        distance = haversine_distances(coords[np.asarray(origins, dtype=int)], coords) * 6371.0088
        kernel = np.exp(-0.5 * (distance / sigma) ** 2) * grid.areas[None, :]
        return kernel / kernel.sum(axis=1, keepdims=True)

    @classmethod
    def fit(cls, events, grid, start, end, m0, progress=lambda p: None):
        mask = (events["t"] >= start) & (events["t"] < end) & (events["m"] >= m0)
        t, cells, mag = (events[k][mask] for k in ("t", "cell", "m"))
        if not 40 <= len(t) <= 1800:
            raise ValueError(
                f"ETAS needs 40–1800 target-threshold events in the declared fit window; received {len(t)}"
            )
        beta = 1 / max(float((mag - m0).mean()), 0.1)
        unique, inverse = np.unique(cells, return_inverse=True)
        background = (
            grid.areas / grid.areas.sum()
        )  # Uniform per area: independently specified, not fitted to future labels.
        lag = t[:, None] - t[None, :]
        prior = lag > 0
        # Precompute exact cell-centroid haversine distances for likelihood kernel.
        coords = np.radians(grid.lonlat[:, ::-1])
        distances = haversine_distances(coords[unique], coords) * 6371.0088
        duration = end - start

        def loss(theta):
            mu, n, ratio, c, p, sigma = theta
            alpha = ratio * beta
            k = productivity(mag, m0, n, alpha, beta)
            spatial = np.exp(-0.5 * (distances / sigma) ** 2) * grid.areas[None, :]
            spatial /= spatial.sum(axis=1, keepdims=True)
            # rows = children, columns = preceding parents
            pair = spatial[inverse][:, cells].T
            temporal = (p - 1) / c * (1 + np.maximum(lag, 0) / c) ** (-p) * prior
            intensity = mu * background[cells] + (temporal * pair * k[None, :]).sum(axis=1)
            integral = mu * duration + np.sum(k * omori_cdf(end - t, c, p))
            return float(integral - np.log(np.maximum(intensity, 1e-300)).sum())

        rate = len(t) / duration
        opt = minimize(
            loss,
            [rate * 0.6, 0.35, 0.35, 0.05, 1.4, 70],
            method="L-BFGS-B",
            bounds=[(1e-5, max(rate * 3, 1)), (0.001, 0.85), (0.01, 0.8), (0.005, 1), (1.05, 2.5), (25, 250)],
            options={"maxiter": 100, "ftol": 1e-8},
        )
        progress(1)
        if not opt.success or not np.isfinite(opt.fun):
            raise ValueError(f"ETAS optimizer unavailable: {opt.message}; iterations={opt.nit}")
        mu, n, ratio, c, p, sigma = opt.x
        return cls(
            float(mu),
            float(n),
            float(ratio * beta),
            beta,
            float(c),
            float(p),
            float(sigma),
            m0,
            background,
            {
                "converged": bool(opt.success),
                "negative_log_likelihood": float(opt.fun),
                "iterations": int(opt.nit),
                "fit_events": len(t),
                "fit_days": duration,
                "branching_ratio": float(n),
                "boundary": "finite domain normalized offspring; no outside triggering",
                "coordinate_units": "haversine km at cell centroids",
                "history": "empty at fit start; prediction parents truncated at 90 days",
                "external_validation": "pending",
                "spatial_approximation": "Gaussian probability masses on 50-km cells, gamma fixed 0",
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
        rng = np.random.default_rng(self.seed)
        # cutoff precedes midnight by 30 min; simulate the unobserved gap as well.
        start = cutoff if horizon_start is None else horizon_start
        horizon = start + 1
        mask = (events["t"] < cutoff) & (events["t"] >= cutoff - 90) & (events["m"] >= self.m0)
        parents = list(zip(events["t"][mask], events["cell"][mask], events["m"][mask]))
        counts = np.zeros(len(grid.ids))
        hits = np.zeros(len(grid.ids))
        spatial_cache = {}

        def spatial(cell):
            if cell not in spatial_cache:
                spatial_cache[cell] = self.spatial(grid, [cell], self.sigma_km)[0]
            return spatial_cache[cell]

        for simulation in range(self.simulations):
            background_n = rng.poisson(self.mu * (horizon - cutoff))
            generated = [
                (float(t), int(cell), float(m))
                for t, cell, m in zip(
                    rng.uniform(cutoff, horizon, background_n),
                    rng.choice(len(grid.ids), background_n, p=self.background),
                    self.m0 + rng.exponential(1 / self.beta, background_n),
                )
            ]
            queue = parents + generated
            cursor = 0
            while cursor < len(queue):
                parent_time, cell, mag = queue[cursor]
                cursor += 1
                lo = max(0, cutoff - parent_time)
                hi = horizon - parent_time
                f0, f1 = omori_cdf(lo, self.c, self.p), omori_cdf(hi, self.c, self.p)
                expected = productivity(mag, self.m0, self.branching, self.alpha, self.beta) * (f1 - f0)
                n = rng.poisson(expected)
                if len(queue) + n > 20000:
                    raise ValueError(
                        "ETAS simulation resource limit reached; no partial probabilities published"
                    )
                if n:
                    lags = self.c * ((1 - rng.uniform(f0, f1, n)) ** (1 / (1 - self.p)) - 1)
                    children = list(
                        zip(
                            parent_time + lags,
                            rng.choice(len(grid.ids), n, p=spatial(int(cell))),
                            self.m0 + rng.exponential(1 / self.beta, n),
                        )
                    )
                    generated.extend(children)
                    queue.extend(children)
            samples = [int(c) for t, c, m in generated if start <= t < horizon]
            histogram = np.bincount(samples, minlength=len(grid.ids))
            counts += histogram
            hits += histogram > 0
            if simulation % 32 == 0:
                progress(simulation / self.simulations)
        # Raw Monte Carlo hit frequencies, no invented Poisson ETAS conversion.
        # Jeffreys smoothing is recorded explicitly, including finite-simulation uncertainty.
        probabilities = (hits + 0.5) / (self.simulations + 1)
        return probabilities, counts / self.simulations
