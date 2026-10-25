"""Bounded chronological fitting, tuning, calibration, and saved artifacts."""

import io
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

from .archive import save_model
from .artifacts import digest, put
from .catalogue import event_arrays, has_coverage, load_experiment, make_snapshot
from .db import transaction
from .etas import ETAS
from .features import NAMES, SCHEMA, features, labels
from .rate import HistoricalRate
from .tables import FeatureSet, now
from .training_inputs import validate_training_inputs

TRAINING_RECIPE = "chronological-windows-v2"


def logit(p):
    p = np.clip(p, 1e-8, 1 - 1e-8)
    return np.log(p / (1 - p)).reshape(-1, 1)


def train_suite(
    experiment_id, cutoff, fit_days=180, tune_days=30, calibration_days=30, progress=lambda p: None
):
    receipt_cutoff = now()
    cutoff = validate_training_inputs(cutoff, receipt_cutoff, fit_days, tune_days, calibration_days)
    experiment, grid = load_experiment(experiment_id)
    calibration_start = cutoff - timedelta(days=calibration_days)
    tune_start = calibration_start - timedelta(days=tune_days)
    fit_start = tune_start - timedelta(days=fit_days)
    first_row_cutoff = fit_start - timedelta(minutes=30)
    warm_start = first_row_cutoff - timedelta(days=90)
    etas_start = max(fit_start, tune_start - timedelta(days=90))
    total_days = fit_days + tune_days + calibration_days
    if total_days * len(grid.ids) > 4_000_000:
        raise ValueError(
            "Training resource limit is four million cell-days; reduce partitions or coarsen a new grid"
        )
    partitions = {
        "fit": [fit_start.isoformat(), tune_start.isoformat()],
        "tune": [tune_start.isoformat(), calibration_start.isoformat()],
        "calibration": [calibration_start.isoformat(), cutoff.isoformat()],
        "untouched_test": "strictly after training cutoff; never consumed here",
        "availability": "retrospective_revised",
        "grouping": "all cells on a date stay together",
    }
    snapshot, rows = make_snapshot(
        experiment.source_id, receipt_cutoff, "retrospective_revised", origin_before=cutoff
    )
    events = event_arrays(rows, grid, experiment.config)
    target = experiment.config["target_magnitude"]
    coverage_identity = {
        "start": warm_start.isoformat(),
        "end": cutoff.isoformat(),
        "complete": has_coverage(experiment.source_id, warm_start, cutoff, cutoff=snapshot.cutoff),
        "policy": "complete_retrieval_asof_training_snapshot",
    }
    common = {
        "feature_schema": SCHEMA,
        "features": NAMES,
        "partitions": partitions,
        "random_seed": 2026,
        "negative_sampling": False,
        "class_weights": None,
        "early_stopping": False,
        "thread_limit": 2,
        "training_recipe": TRAINING_RECIPE,
        "training_coverage": dict(coverage_identity, receipt_cutoff=snapshot.cutoff.isoformat()),
    }
    common["training_signature"] = digest(
        {
            "experiment": experiment.config_hash,
            "partitions": partitions,
            "revisions": snapshot.revision_ids,
            "schema": SCHEMA,
            "recipe": TRAINING_RECIPE,
            # Coverage-only repairs can change availability without changing
            # any revision ID. The receipt cutoff is audit metadata, not a
            # volatile dependency that invalidates identical complete fits.
            "coverage": coverage_identity,
        }
    )
    from sqlalchemy import select

    from .tables import Model

    with transaction() as s:
        existing = list(
            s.scalars(
                select(Model).where(
                    Model.experiment_id == experiment_id,
                    Model.metadata_["training_signature"].as_string() == common["training_signature"],
                )
            )
        )
    if len(existing) == 4:
        return existing
    available = []

    def window(start, end, role, **details):
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_days": (end - start).total_seconds() / 86400,
            "role": role,
            **details,
        }

    def event_count(start, end, *, target_only=False):
        mask = (events["t"] >= start.timestamp() / 86400) & (events["t"] < end.timestamp() / 86400)
        if target_only:
            mask &= events["m"] >= target
        return int(mask.sum())

    def supervised_window(start, end, role, partition):
        details = {"cell_day_count": (end - start).days * len(grid.ids)}
        if "positive_counts" in common:
            details["positive_cell_day_count"] = common["positive_counts"][partition]
        return window(start, end, role, **details)

    def training_metadata(algorithm, *, completed=True, **details):
        windows = dict.fromkeys(("fit", "context", "selection", "calibration", "diagnostic"))
        if algorithm == "etas_discrete_space":
            windows["fit"] = window(
                etas_start,
                tune_start,
                "conditional_point_process_fit_empty_initial_history",
                event_count=event_count(etas_start, tune_start, target_only=True),
            )
        elif algorithm == "historical_rate":
            windows["fit"] = window(
                fit_start,
                tune_start,
                "target_event_rate_fit",
                event_count=event_count(fit_start, tune_start, target_only=True),
            )
            windows["selection"] = supervised_window(
                tune_start, calibration_start, "rate_hyperparameter_selection", "tune"
            )
        else:
            hybrid = algorithm == "calibrated_hybrid"
            windows["fit"] = supervised_window(
                fit_start,
                tune_start,
                "rate_and_classifier_component_fit" if hybrid else "histogram_gradient_boosting_fit",
                "fit",
            )
            if hybrid:
                windows["fit"]["event_count"] = event_count(fit_start, tune_start, target_only=True)
            # Context is the union of inputs to generated feature rows, not an
            # extra parameter-fitting block. Each row still stops 30m before
            # its own target start, including later diagnostic/calibration rows.
            context_end = cutoff - timedelta(days=1, minutes=30)
            windows["context"] = window(
                warm_start,
                context_end,
                "per_row_feature_inputs",
                event_count=event_count(warm_start, context_end),
                warmup_end=first_row_cutoff.isoformat(),
                first_row_cutoff=first_row_cutoff.isoformat(),
                per_row_cutoff_minutes=30,
            )
            windows["selection" if hybrid else "diagnostic"] = supervised_window(
                tune_start,
                calibration_start,
                "mixture_weight_and_rate_hyperparameter_selection"
                if hybrid
                else "raw_classifier_brier_diagnostic_only",
                "tune",
            )
            windows["calibration"] = supervised_window(
                calibration_start,
                cutoff,
                "sigmoid_fit_on_selected_mixture" if hybrid else "sigmoid_fit_on_raw_classifier",
                "calibration",
            )
        return dict(
            common,
            training_windows=windows,
            training_windows_status="completed" if completed else "planned_unavailable",
            **details,
        )

    def unavailable(algorithm, reason):
        model = save_model(
            experiment,
            algorithm,
            None,
            cutoff,
            snapshot.id,
            training_metadata(algorithm, completed=False, reason=str(reason)),
            "insufficient_data",
        )
        available.append(model)

    if not len(events["t"]) or not coverage_identity["complete"]:
        for algorithm in (
            "historical_rate",
            "hist_gradient_boosting",
            "calibrated_hybrid",
            "etas_discrete_space",
        ):
            unavailable(
                algorithm,
                "Complete retrieval coverage is required for fit, tune, calibration and 90-day warmup",
            )
        return available
    history_start = warm_start.timestamp() / 86400
    xs, ys, day_arrays = [], [], []
    feature_refs = []
    for day in range(total_days):
        start = fit_start + timedelta(days=day)
        day_number = start.timestamp() / 86400
        x = features(events, grid, day_number - 30 / 1440, target, history_start)
        y = labels(events, len(grid.ids), day_number, target)
        xs.append(x)
        ys.append(y)
        day_arrays.append(day_number)
        # Persist weekly bounded batches; data are inspectable and reproducible.
        if day % 7 == 6 or day == total_days - 1:
            first = day - day % 7
            frame = pd.DataFrame(np.concatenate(xs[first : day + 1]), columns=NAMES)
            frame["y"] = np.concatenate(ys[first : day + 1])
            frame["day"] = np.repeat(day_arrays[first : day + 1], len(grid.ids))
            frame["cell_id"] = grid.ids * (day - first + 1)
            buffer = io.BytesIO()
            frame.to_parquet(buffer, index=False)
            feature_refs.append(put(buffer.getvalue(), "parquet"))
        progress(0.05 + 0.25 * (day + 1) / total_days)
    x_fit = np.concatenate(xs[:fit_days])
    y_fit = np.concatenate(ys[:fit_days])
    x_tune = np.concatenate(xs[fit_days : fit_days + tune_days])
    y_tune = np.concatenate(ys[fit_days : fit_days + tune_days])
    x_cal = np.concatenate(xs[fit_days + tune_days :])
    y_cal = np.concatenate(ys[fit_days + tune_days :])
    del xs, ys
    feature_manifest = put(__import__("json").dumps(feature_refs).encode(), "json")
    with transaction() as s:
        s.add(
            FeatureSet(
                experiment_id=experiment_id,
                schema_version=SCHEMA,
                artifact=feature_manifest,
                partitions=partitions,
            )
        )
    common["feature_manifest"] = feature_manifest
    common["positive_counts"] = {
        "fit": int(y_fit.sum()),
        "tune": int(y_tune.sum()),
        "calibration": int(y_cal.sum()),
    }
    common["cell_day_counts"] = {"fit": len(y_fit), "tune": len(y_tune), "calibration": len(y_cal)}
    try:
        candidates = []
        for sigma in (0.6, 1.4, 2.5):
            for shrinkage in (0.02, 0.1):
                model = HistoricalRate.fit(
                    events,
                    grid,
                    fit_start.timestamp() / 86400,
                    tune_start.timestamp() / 86400,
                    target,
                    sigma,
                    shrinkage,
                )
                loss = np.mean((np.tile(model.predict(), tune_days) - y_tune) ** 2)
                candidates.append((float(loss), model))
        tuning_score, rate = min(candidates, key=lambda pair: pair[0])
        rate_model = save_model(
            experiment,
            "historical_rate",
            rate,
            cutoff,
            snapshot.id,
            training_metadata(
                "historical_rate",
                **rate.diagnostics,
                tuning_brier=tuning_score,
                selection="six smoothing/shrinkage settings on later tuning block",
            ),
        )
        available.append(rate_model)
    except ValueError as exc:
        for algorithm in (
            "historical_rate",
            "hist_gradient_boosting",
            "calibrated_hybrid",
            "etas_discrete_space",
        ):
            unavailable(algorithm, exc)
        return available
    progress(0.4)
    if min(int(y_fit.sum()), int(y_tune.sum()), int(y_cal.sum())) < 20 or any(
        len(np.unique(y)) < 2 for y in (y_fit, y_tune, y_cal)
    ):
        unavailable(
            "hist_gradient_boosting",
            "At least 20 positives and both classes in each chronological partition required",
        )
        unavailable("calibrated_hybrid", "ML component unavailable")
    else:
        with threadpool_limits(limits=2):
            classifier = HistGradientBoostingClassifier(
                max_iter=60,
                max_leaf_nodes=15,
                max_depth=4,
                min_samples_leaf=50,
                max_bins=63,
                learning_rate=0.08,
                l2_regularization=2,
                early_stopping=False,
                random_state=2026,
            )
            classifier.fit(x_fit, y_fit)
            ml_tune = classifier.predict_proba(x_tune)[:, 1]
            ml_cal = classifier.predict_proba(x_cal)[:, 1]
            calibration = LogisticRegression(C=1, solver="lbfgs", random_state=2026)
            calibration.fit(logit(ml_cal), y_cal)
            ml_artifact = {
                "kind": "ml",
                "classifier": classifier,
                "calibration": calibration,
                "history_start": history_start,
            }
            ml_model = save_model(
                experiment,
                "hist_gradient_boosting",
                ml_artifact,
                cutoff,
                snapshot.id,
                training_metadata(
                    "hist_gradient_boosting",
                    hyperparameters=classifier.get_params(),
                    selection="fixed hyperparameters; tuning Brier is diagnostic only",
                    calibration="sigmoid on independent later calibration block",
                    tuning_brier=float(np.mean((ml_tune - y_tune) ** 2)),
                ),
            )
            available.append(ml_model)
            base_tune = np.tile(rate.predict(), tune_days)
            weights = np.linspace(0, 1, 21)
            weight = float(
                min(weights, key=lambda w: np.mean(((1 - w) * base_tune + w * ml_tune - y_tune) ** 2))
            )
            blended_cal = (1 - weight) * np.tile(rate.predict(), calibration_days) + weight * ml_cal
            hybrid_cal = LogisticRegression(C=1, solver="lbfgs", random_state=2026).fit(
                logit(blended_cal), y_cal
            )
            hybrid = {
                "kind": "hybrid",
                "classifier": classifier,
                "rate": rate,
                "weight_ml": weight,
                "calibration": hybrid_cal,
                "history_start": history_start,
            }
            available.append(
                save_model(
                    experiment,
                    "calibrated_hybrid",
                    hybrid,
                    cutoff,
                    snapshot.id,
                    training_metadata(
                        "calibrated_hybrid",
                        components=[rate_model.id, ml_model.id],
                        component_algorithms=["historical_rate", "raw hist_gradient_boosting"],
                        weights={"historical_rate": 1 - weight, "ml": weight},
                        etas_included=False,
                        calibration="independent calibration block sigmoid; weights selected on earlier tuning block",
                    ),
                )
            )
    progress(0.75)
    try:
        # Fit only an explicitly bounded final 90-day section of the FIT interval.
        etas = ETAS.fit(
            events,
            grid,
            etas_start.timestamp() / 86400,
            tune_start.timestamp() / 86400,
            target,
            lambda _: progress(0.9),
        )
        available.append(
            save_model(
                experiment,
                "etas_discrete_space",
                etas,
                cutoff,
                snapshot.id,
                training_metadata(
                    "etas_discrete_space",
                    **etas.diagnostics,
                    simulations=etas.simulations,
                    smoothing="Jeffreys (hits+0.5)/(simulations+1)",
                    max_monte_carlo_standard_error=0.5 / np.sqrt(etas.simulations),
                ),
            )
        )
    except ValueError as exc:
        unavailable("etas_discrete_space", exc)
    progress(1)
    return available


def predict_artifact(artifact, events, grid, cutoff, config, progress=lambda p: None):
    if isinstance(artifact, ETAS):
        return artifact.predict(events, grid, cutoff, horizon_start=cutoff + 30 / 1440, progress=progress)
    x = features(events, grid, cutoff, config["target_magnitude"], artifact["history_start"])
    with threadpool_limits(limits=2):
        raw = artifact["classifier"].predict_proba(x)[:, 1]
        if artifact["kind"] == "hybrid":
            weight = artifact["weight_ml"]
            raw = (1 - weight) * artifact["rate"].predict() + weight * raw
        probability = artifact["calibration"].predict_proba(logit(raw))[:, 1]
    return probability, None
