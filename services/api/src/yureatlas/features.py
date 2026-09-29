"""Temporal cell-day features. Every feature accepts an explicit origin cutoff."""

import numpy as np
from scipy.ndimage import gaussian_filter
from sklearn.neighbors import BallTree

SCHEMA = "cell-day-v1"
NAMES = [
    "count_1d",
    "count_7d",
    "count_30d",
    "count_90d",
    "target_7d",
    "target_30d",
    "days_since_cell_event",
    "max_magnitude_30d",
    "mean_depth_30d_km",
    "depth_missing",
    "neighbor_7d",
    "nearest_7d_km",
    "expanding_background_daily",
    "activity_ratio",
]


def features(events, grid, cutoff: float, target: float, history_start: float):
    n = len(grid.ids)
    eligible = (events["t"] < cutoff) & (events["t"] >= history_start)
    t, cell, mag, depth = (events[k][eligible] for k in ("t", "cell", "m", "depth"))
    values = []
    counts = {}
    for window in (1, 7, 30, 90):
        counts[window] = np.bincount(cell[t >= cutoff - window], minlength=n).astype(float)
        values.append(counts[window])
    for window in (7, 30):
        values.append(np.bincount(cell[(t >= cutoff - window) & (mag >= target)], minlength=n))
    recent = np.full(n, np.nan)
    if len(t):
        recent.fill(-np.inf)
        np.maximum.at(recent, cell, t)
        recent = cutoff - recent
        recent[~np.isfinite(recent)] = np.nan
    values.append(recent)
    mask = t >= cutoff - 30
    maximum = np.full(n, -np.inf)
    np.maximum.at(maximum, cell[mask], mag[mask])
    maximum[~np.isfinite(maximum)] = np.nan
    values.append(maximum)
    known = mask & np.isfinite(depth)
    depth_count = np.bincount(cell[known], minlength=n)
    depth_sum = np.bincount(cell[known], weights=depth[known], minlength=n)
    values.append(np.divide(depth_sum, depth_count, out=np.full(n, np.nan), where=depth_count > 0))
    values.append((depth_count == 0).astype(float))
    width = len({i for i, _ in grid.indices})
    values.append(gaussian_filter(counts[7].reshape(-1, width), 1, mode="constant").ravel())
    recent_cells = np.unique(cell[t >= cutoff - 7])
    if len(recent_cells):
        # Haversine distances between cell centroids, explicitly in km. Cell
        # resolution bounds precision; no projected metres called ground distance.
        coords = np.radians(grid.lonlat[:, ::-1])
        tree = BallTree(coords[recent_cells], metric="haversine")
        distance = tree.query(coords, k=1)[0].ravel() * 6371.0088
    else:
        distance = np.full(n, np.nan)
    values.append(distance)
    target_counts = np.bincount(cell[mag >= target], minlength=n).astype(float)
    smoothed = gaussian_filter(target_counts.reshape(-1, width), 1.4, mode="constant").ravel()
    background = (smoothed + 0.05 * (target_counts.sum() + 1) * grid.areas / grid.areas.sum()) / max(
        cutoff - history_start, 1
    )
    values.append(background)
    values.append(counts[7] / (7 * background + 1))
    return np.column_stack(values).astype(np.float32)


def labels(events, n_cells, start: float, target: float):
    mask = (events["t"] >= start) & (events["t"] < start + 1) & (events["m"] >= target)
    return (np.bincount(events["cell"][mask], minlength=n_cells) > 0).astype(np.int8)
