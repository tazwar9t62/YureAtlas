import io
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import joblib
import numpy as np
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError

from .artifacts import digest, put, read
from .catalogue import event_arrays, has_coverage, load_experiment, make_snapshot
from .db import transaction
from .eligibility import eligible_cell
from .metrics import score
from .rate import HistoricalRate
from .tables import Evaluation, EvaluationCell, Forecast, ForecastCell, Model, Snapshot, now, uid
from .timeutil import utc


def save_model(experiment, algorithm, artifact, cutoff, snapshot_id, metadata, status="available"):
    if metadata.get("training_signature"):
        with transaction() as s:
            existing = s.scalar(
                select(Model).where(
                    Model.experiment_id == experiment.id,
                    Model.algorithm == algorithm,
                    Model.metadata_["training_signature"].as_string() == metadata["training_signature"],
                )
            )
            if existing:
                return existing
    ref = None
    if artifact is not None:
        buffer = io.BytesIO()
        joblib.dump(artifact, buffer, compress=3)
        ref = put(buffer.getvalue(), "joblib")
    with transaction() as s:
        model = Model(
            experiment_id=experiment.id,
            algorithm=algorithm,
            status=status,
            training_cutoff=cutoff,
            available_at=now(),
            snapshot_id=snapshot_id,
            artifact=ref,
            metadata_=metadata,
        )
        s.add(model)
        s.flush()
        return model


def load_model(model):
    if model.status != "available" or not model.artifact:
        raise ValueError("Model unavailable: " + model.metadata_.get("reason", model.status))
    # Only DB-referenced, locally produced, checksum-verified artifacts are loaded.
    # There is intentionally no model-file upload endpoint.
    return joblib.load(io.BytesIO(read(model.artifact)))


def code_provenance():
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = os.environ.get("CODE_VERSION", "unavailable")
    manifest = Path("uv.lock")
    source_hash = digest({p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))})
    return {
        "commit": commit,
        "source_tree_sha256": source_hash,
        "dependency_manifest": put(manifest.read_bytes(), "txt") if manifest.exists() else None,
    }


def train_rate(experiment_id, cutoff, history_start=None):
    experiment, grid = load_experiment(experiment_id)
    snapshot, rows = make_snapshot(experiment.source_id, now(), "retrospective_revised", origin_before=cutoff)
    events = event_arrays(rows, grid, experiment.config)
    if len(events["t"]) < 5:
        return save_model(
            experiment,
            "historical_rate",
            None,
            cutoff,
            snapshot.id,
            {"reason": "Insufficient catalogue records"},
            "insufficient_data",
        )
    end = cutoff.timestamp() / 86400
    start = history_start.timestamp() / 86400 if history_start else float(events["t"].min())
    artifact = HistoricalRate.fit(events, grid, start, end, experiment.config["target_magnitude"])
    return save_model(
        experiment,
        "historical_rate",
        artifact,
        cutoff,
        snapshot.id,
        dict(artifact.diagnostics, feature_schema="rate-v1", selection="fixed initial settings"),
    )


def _validate_publication_time(model, start, cutoff, issue, mode, stage):
    if cutoff > start or model.training_cutoff > cutoff:
        raise ValueError("Training and feature cutoffs must precede the prediction window")
    if mode != "prospective":
        return
    if issue >= start:
        raise ValueError(f"Missed publication deadline {stage}; nothing published")
    if issue < cutoff:
        raise ValueError("Future input cutoff; choose a genuinely future window")
    if model.available_at > issue:
        raise ValueError("Model artifact unavailable at issuance; nothing published")


def _validated_output(probabilities, expected, cell_count):
    try:
        probabilities = np.asarray(probabilities, dtype=float)
        expected = np.asarray(expected, dtype=float) if expected is not None else None
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid or incomplete model output; nothing published") from error
    if (
        probabilities.shape != (cell_count,)
        or not np.isfinite(probabilities).all()
        or ((probabilities < 0) | (probabilities > 1)).any()
        or (
            expected is not None
            and (expected.shape != (cell_count,) or not np.isfinite(expected).all() or (expected < 0).any())
        )
    ):
        raise ValueError("Invalid or incomplete model output; nothing published")
    return probabilities, expected


def issue_forecast(
    experiment_id, model_id, start, *, mode="prospective", cutoff=None, progress=lambda p: None
):
    start = utc(start)
    cutoff = utc(cutoff) if cutoff is not None else start - timedelta(minutes=30)
    experiment, grid = load_experiment(experiment_id)
    if mode not in {"prospective", "retrospective_revised", "synthetic_demo"}:
        raise ValueError(
            "Replay-asof is unavailable without archived fitted artifacts at the historical issue time"
        )
    if experiment.synthetic != (mode == "synthetic_demo"):
        raise ValueError("Synthetic and real forecasts must remain separate")
    if cutoff != start - timedelta(minutes=30):
        raise ValueError("This feature schema requires the input cutoff exactly 30 minutes before the window")
    # Legacy direct Python callers could hash an equivalent non-UTC start into
    # another key. Recognize their immutable rows without rewriting old keys.
    existing_query = (
        select(Forecast)
        .where(
            Forecast.experiment_id == experiment_id,
            Forecast.model_id == model_id,
            Forecast.start == start,
            Forecast.mode == mode,
        )
        .order_by(Forecast.created_at, Forecast.id)
        .limit(1)
    )
    with transaction() as s:
        model = s.get(Model, model_id)
        if not model or model.experiment_id != experiment_id:
            raise ValueError("Model and experiment are incompatible")
        key = digest({"experiment": experiment_id, "model": model_id, "start": start, "mode": mode})
        existing = s.scalar(existing_query)
        if existing:
            return existing
    _validate_publication_time(model, start, cutoff, now(), mode, "before computation")
    if mode == "prospective":
        from .tables import Source

        with transaction() as s:
            source = s.get(Source, experiment.source_id)
            source_failed = source.status == "failed"
        if source_failed or not has_coverage(
            experiment.source_id, cutoff - timedelta(days=1), cutoff - timedelta(minutes=30), cutoff=cutoff
        ):
            raise ValueError("Recent input coverage unavailable; forecast withheld")
    snapshot_cutoff = cutoff if mode == "prospective" else now()
    snapshot, rows = make_snapshot(experiment.source_id, snapshot_cutoff, mode, origin_before=cutoff)
    rows = [r for r in rows if r.origin_time < cutoff]
    events = event_arrays(rows, grid, experiment.config)
    artifact = load_model(model)
    if isinstance(artifact, HistoricalRate):
        probabilities = artifact.predict()
        expected = artifact.rates
    else:
        from .learning import predict_artifact

        probabilities, expected = predict_artifact(
            artifact, events, grid, cutoff.timestamp() / 86400, experiment.config, progress
        )
    probabilities, expected = _validated_output(probabilities, expected, len(grid.ids))
    forecast_id = uid()
    values = [
        {
            "forecast_id": forecast_id,
            "cell_id": cell,
            "probability": float(p),
            "expected_count": float(expected[i]) if expected is not None else None,
            "band": "red"
            if p >= experiment.config["red"]
            else "yellow"
            if p >= experiment.config["yellow"]
            else "green",
        }
        for i, (cell, p) in enumerate(zip(grid.ids, probabilities))
    ]
    # Hashing source/dependencies and persisting their artifacts can take time;
    # finish that work before entering the publication transaction.
    provenance = {
        "experiment_hash": experiment.config_hash,
        "grid_hash": grid.hash,
        "model_artifact": model.artifact,
        "model_training_cutoff": model.training_cutoff.isoformat(),
        "feature_schema": model.metadata_.get("feature_schema", "v1"),
        "snapshot_id": snapshot.id,
        "availability_policy": mode,
        "seed": 2026,
        "thresholds": {"yellow": experiment.config["yellow"], "red": experiment.config["red"]},
        "cell_count": len(values),
        **code_provenance(),
    }
    try:
        with transaction() as s:
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
            existing = s.scalar(existing_query)
            if existing:
                return existing
            # This is an issuance timestamp inside the transaction, not a claim
            # about physical commit visibility. Revalidate after any lock wait.
            issue = now()
            _validate_publication_time(model, start, cutoff, issue, mode, "during computation or publication")
            s.execute(insert(ForecastCell), values)
            forecast = Forecast(
                id=forecast_id,
                logical_key=key,
                experiment_id=experiment_id,
                model_id=model_id,
                snapshot_id=snapshot.id,
                issued_at=issue,
                start=start,
                end=start + timedelta(days=1),
                input_cutoff=cutoff,
                mode=mode,
                status="published",
                provenance=provenance,
            )
            s.add(forecast)
            s.flush()
            _validate_publication_time(model, start, cutoff, now(), mode, "after publication flush")
            # The initially deferred DB trigger also checks its own wall clock
            # at transaction finalization, including delays after this flush.
            return forecast
    except DBAPIError as error:
        if (
            getattr(error.orig, "sqlstate", None) == "23514"
            and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            == "prospective_publication_deadline"
        ):
            raise ValueError(
                "Missed publication deadline during transaction finalization; nothing published"
            ) from error
        raise


def evaluate_forecast(forecast_id, *, asof=None):
    asof = asof or now()
    with transaction() as s:
        forecast = s.get(Forecast, forecast_id)
        if not forecast:
            raise ValueError("Unknown forecast")
    if forecast.end > asof:
        raise ValueError("Cannot evaluate an unfinished forecast window")
    experiment, grid = load_experiment(forecast.experiment_id)
    snapshot, rows = make_snapshot(
        experiment.source_id,
        asof,
        "evaluation_revised",
        origin_before=forecast.end,
        origin_after=forecast.start,
    )
    covered = has_coverage(experiment.source_id, forecast.start, forecast.end, cutoff=asof)
    status = "provisional" if covered else "awaiting_data"
    with transaction() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": forecast_id})
        previous = s.scalar(
            select(Evaluation)
            .where(Evaluation.forecast_id == forecast_id)
            .order_by(Evaluation.version.desc())
            .limit(1)
        )
        if previous:
            old_snapshot = s.get(Snapshot, previous.snapshot_id)
            if previous.status == status and old_snapshot.revision_ids == snapshot.revision_ids:
                return previous
        cells = {
            c.cell_id: c
            for c in s.scalars(select(ForecastCell).where(ForecastCell.forecast_id == forecast_id))
        }
        counts = np.zeros(len(grid.ids), dtype=int)
        for event in rows:
            if forecast.start <= event.origin_time < forecast.end:
                cell = eligible_cell(
                    event, grid, experiment.config, minimum=experiment.config["target_magnitude"]
                )
                if cell is not None:
                    counts[grid.lookup[cell]] += 1
        probabilities = np.array([cells[c].probability for c in grid.ids])
        metrics = score(
            probabilities if covered else [],
            (counts > 0).astype(int) if covered else [],
            grid.areas if covered else [],
            threshold=experiment.config["red"],
            missing=0 if covered else len(grid.ids),
        )
        metrics["source_policy"] = "retrieval coverage only; no automatic catalogue finalization"
        evaluation_id = uid()
        if covered:
            s.execute(
                insert(EvaluationCell),
                [
                    {
                        "evaluation_id": evaluation_id,
                        "cell_id": cell,
                        "observed_count": int(counts[i]),
                        "outcome": ("TP" if counts[i] else "FP")
                        if cells[cell].band == "red"
                        else ("FN" if counts[i] else "TN"),
                    }
                    for i, cell in enumerate(grid.ids)
                ],
            )
        evaluation = Evaluation(
            id=evaluation_id,
            forecast_id=forecast_id,
            snapshot_id=snapshot.id,
            version=previous.version + 1 if previous else 1,
            status=status,
            reason="Catalogue/coverage reconciliation" if previous else "Initial completed-window evaluation",
            metrics=metrics,
        )
        s.add(evaluation)
        s.flush()
        return evaluation
