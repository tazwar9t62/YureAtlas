from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter


@dataclass
class HistoricalRate:
    rates: np.ndarray
    sigma_cells: float
    shrinkage: float
    training_days: float
    algorithm: str = "historical_rate"

    @classmethod
    def fit(cls, events, grid, start: float, end: float, target: float, sigma=1.4, shrinkage=0.05):
        if end <= start:
            raise ValueError("Positive training duration required")
        eligible = (events["t"] >= start) & (events["t"] < end) & (events["m"] >= target)
        counts = np.bincount(events["cell"][eligible], minlength=len(grid.ids)).astype(float)
        if counts.sum() < 5:
            raise ValueError("Insufficient data: at least five target events required")
        width = len({i for i, _ in grid.indices})
        smoothed = gaussian_filter(counts.reshape(-1, width), sigma=sigma, mode="constant").ravel()
        # Boundary loss is renormalized; shrinkage shares positive mass with every
        # retained cell, proportional to its actual (clipped) area.
        smoothed *= counts.sum() / max(smoothed.sum(), 1e-12)
        rates = ((1 - shrinkage) * smoothed + shrinkage * counts.sum() * grid.areas / grid.areas.sum()) / (
            end - start
        )
        return cls(rates, sigma, shrinkage, end - start)

    def predict(self, hours=24):
        return -np.expm1(-self.rates * hours / 24)

    @property
    def diagnostics(self):
        return {
            "assumption": "Poisson occurrence probability 1-exp(-lambda)",
            "smoothing": "Gaussian on EPSG:6933 grid; retained-domain renormalization",
            "sigma_cells": self.sigma_cells,
            "shrinkage": self.shrinkage,
            "training_days": self.training_days,
            "regional_daily_rate": float(self.rates.sum()),
        }
