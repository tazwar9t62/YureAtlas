"""A real saved ETAS failure must not publish partial archived probabilities."""

from collections import deque
from datetime import timedelta
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import func, select
from yureatlas import archive
from yureatlas.artifacts import read
from yureatlas.catalogue import ensure_experiment, load_experiment, make_snapshot
from yureatlas.db import transaction
from yureatlas.etas import ETAS
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Forecast, ForecastCell, Model
from yureatlas.timeutil import utc

pytestmark = pytest.mark.integration


class CapAfterOneSimulation:
    def __init__(self):
        # One immigrant, no children, then an oversized immigrant draw. This
        # reaches failure after a real successful simulation has accumulated.
        self.draws = deque([1, 0, 20_001])
        self.uniform_sizes = []

    def poisson(self, mean):
        assert np.isfinite(mean) and mean >= 0
        return self.draws.popleft()

    def uniform(self, low, high, size):
        self.uniform_sizes.append(size)
        assert size <= 1, "Oversized draw must fail before allocating event arrays"
        return np.full(size, (low + high) / 2)

    def choice(self, cells, size, p):
        assert size <= 1
        return np.zeros(size, dtype=int)

    def exponential(self, scale, size):
        assert size <= 1
        return np.zeros(size)


@pytest.mark.parametrize("failure", ["resource_cap", "invalid_saved_state"])
def test_failed_saved_etas_publishes_no_forecast_or_cell_rows(monkeypatch, failure):
    identity = "etas_publication_" + uuid4().hex[:12]
    experiment = ensure_experiment(
        ExperimentSpec(
            name=identity,
            provider="jma",
            catalogue_namespace=identity,
            west=140,
            east=141,
            south=35,
            north=36,
        )
    )
    _, grid = load_experiment(experiment.id)
    start = utc("2024-07-15T15:00:00Z")
    training_cutoff = start - timedelta(days=2)
    snapshot, _ = make_snapshot(experiment.source_id, training_cutoff, "retrospective_revised")
    artifact = ETAS(
        mu=1,
        branching=0,
        alpha=0,
        beta=2,
        c=0.02,
        p=2,
        sigma_km=50,
        m0=4,
        background=grid.areas / grid.areas.sum(),
        simulations=2,
        diagnostics={"fixture": identity},
    )
    if failure == "invalid_saved_state":
        # Pickle loading bypasses dataclass construction. Prediction must
        # validate the loaded state as well as constructor arguments.
        artifact.branching = float("inf")
    saved = archive.save_model(
        experiment,
        "etas_discrete_space",
        artifact,
        training_cutoff,
        snapshot.id,
        {"feature_schema": "etas-v1", "fixture": identity},
    )
    before_bytes = read(saved.artifact)
    restored = archive.load_model(saved)
    assert isinstance(restored, ETAS) and restored.simulations == 2
    with transaction() as session:
        before_cell_rows = session.scalar(select(func.count()).select_from(ForecastCell))
    progress = []
    if failure == "resource_cap":
        rng = CapAfterOneSimulation()
        monkeypatch.setattr(np.random, "default_rng", lambda seed: rng)
        message = "ETAS simulation resource limit reached; no partial probabilities published"
    else:
        message = "ETAS"
    # Retrospective mode deliberately isolates simulation/publication failure
    # from prospective coverage and wall-clock deadline guards.
    with pytest.raises(ValueError, match=message):
        archive.issue_forecast(
            experiment.id, saved.id, start, mode="retrospective_revised", progress=progress.append
        )
    if failure == "resource_cap":
        assert progress == [0]  # The first simulation really completed.
        assert not rng.draws and rng.uniform_sizes == [1]
    else:
        assert not progress
    with transaction() as session:
        assert not list(session.scalars(select(Forecast).where(Forecast.experiment_id == experiment.id)))
        assert session.scalar(select(func.count()).select_from(ForecastCell)) == before_cell_rows
        unchanged = session.get(Model, saved.id)
        assert unchanged.artifact == saved.artifact and unchanged.status == "available"
    assert read(saved.artifact) == before_bytes
