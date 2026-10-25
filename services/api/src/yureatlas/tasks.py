from datetime import timedelta

from sqlalchemy import select

from .archive import evaluate_forecast, issue_forecast
from .catalogue import ingest_records, load_experiment
from .db import transaction
from .demo import DEMO_TEST_START, seed_catalogue
from .ingestion import ingest_usgs, real_experiment
from .learning import train_suite
from .tables import Forecast, now
from .timeutil import utc
from .training_inputs import validate_backtest_inputs, validate_training_inputs


def run_backtest(
    experiment_id,
    start,
    test_days=7,
    folds=1,
    fit_days=180,
    tune_days=30,
    calibration_days=30,
    progress=lambda p: None,
):
    start = validate_backtest_inputs(start, now(), test_days, folds, fit_days, tune_days, calibration_days)
    experiment, _ = load_experiment(experiment_id)
    mode = "synthetic_demo" if experiment.synthetic else "retrospective_revised"
    forecasts = []
    model_ids = []
    for fold in range(folds):
        fold_start = start + timedelta(days=fold * test_days)
        models = train_suite(
            experiment_id,
            fold_start - timedelta(minutes=30),
            fit_days,
            tune_days,
            calibration_days,
            lambda p, fold=fold: progress((fold + p * 0.5) / folds),
        )
        model_ids.extend(m.id for m in models)
        eligible = [m for m in models if m.status == "available"]
        if not eligible:
            raise ValueError(
                "No models available for the requested partitions; inspect Model lab diagnostics"
            )
        for day in range(test_days):
            for m in eligible:
                prediction = issue_forecast(
                    experiment_id,
                    m.id,
                    fold_start + timedelta(days=day),
                    mode=mode,
                    progress=lambda p, fold=fold, day=day: progress(
                        (fold + 0.5 + 0.5 * day / test_days) / folds
                    ),
                )
                evaluation = evaluate_forecast(prediction.id)
                forecasts.append(
                    {"forecast": prediction.id, "evaluation": evaluation.id, "status": evaluation.status}
                )
            progress((fold + 0.5 + 0.5 * (day + 1) / test_days) / folds)
    return {
        "experiment_id": experiment_id,
        "mode": mode,
        "models": model_ids,
        "forecasts": forecasts,
        "test_policy": "Fixed saved forecasts; subsequent model design informed by these scores is exploratory",
    }


def execute(kind, payload, progress):
    if kind == "forecast":
        from .forecast_execution import run_forecast

        return run_forecast(payload, progress)
    if kind == "demo":
        experiment = seed_catalogue(lambda p: progress(p * 0.3))
        return run_backtest(
            experiment.id, DEMO_TEST_START, test_days=7, progress=lambda p: progress(0.3 + p * 0.7)
        )
    if kind == "import":
        from .artifacts import read
        from .providers import IMPORT_PARSER_VERSION, parse_csv, parse_jma

        if payload["format"] not in {"jma", "csv"}:
            raise ValueError("Unknown import format")
        parser_version = f"{payload['format']}-v{IMPORT_PARSER_VERSION}"
        if payload.get("parser_version", parser_version) != parser_version:
            raise ValueError("Queued import parser version differs from the running parser")

        experiment, grid = load_experiment(payload["experiment_id"])
        if experiment.config["provider"] != "jma":
            raise ValueError("Local imports currently require a separate JMA experiment")
        raw = read(payload["artifact"])
        rows, report = (
            parse_jma(raw, payload["namespace"])
            if payload["format"] == "jma"
            else parse_csv(raw, payload.get("mapping"))
        )
        from .eligibility import JMA_MAGNITUDE_TYPES
        from .providers import JMA_OTHER_MAGNITUDE_TYPES, finish_import_report, report_import_row

        start, end = utc(payload["start"]), utc(payload["end"])
        if start >= end or end > now():
            raise ValueError("Import requires a completed ordered interval")
        exclusions = {}
        for record in rows:
            if record["_import_outcome"] != "selected":
                continue
            outcome = "policy_excluded"
            if record["status"] == "deleted":
                code, reason = "deleted", "Deleted event correction"
            elif record["event_type"] != "earthquake":
                code, reason = "event_type_excluded", "Non-earthquake event correction"
            elif record["magnitude_type"] not in JMA_MAGNITUDE_TYPES:
                code, reason = "magnitude_family_excluded", "Non-JMA magnitude family"
                if record["magnitude_type"] not in JMA_OTHER_MAGNITUDE_TYPES:
                    outcome, code, reason = (
                        "invalid",
                        "unknown_magnitude_code",
                        "Unknown JMA magnitude family",
                    )
            elif not start <= record["origin_time"] < end:
                code, reason = (
                    "outside_declared_interval",
                    "Origin is outside the declared half-open interval",
                )
            elif record["magnitude"] < experiment.config["input_magnitude"]:
                code, reason = "below_input_magnitude", "Magnitude is below the experiment input threshold"
            elif grid.assign(record["longitude"], record["latitude"]) is None:
                code, reason = "outside_domain", "Origin is outside the experiment domain"
            else:
                continue
            report["selected_count"] -= 1
            report["reason_counts"]["selected"] -= 1
            report["selected"] = [r for r in report["selected"] if r["line"] != record["_import_line"]]
            report_import_row(
                report,
                outcome,
                record["_import_line"],
                code,
                reason,
                end_line=record.get("_import_end_line"),
            )
            record["_import_outcome"] = outcome
            exclusions[code] = exclusions.get(code, 0) + 1
        rows, report = finish_import_report(report, rows)
        report["selected_in_domain"] = report["selected_count"]
        report["selection_exclusions"] = exclusions
        report["raw_ref"] = payload["artifact"]
        declared = payload.get("coverage_declared", False)
        if not isinstance(declared, bool):
            raise ValueError("coverage_declared must be a boolean")
        report["coverage_declared"] = declared
        report["coverage_effect"] = "authoritative"
        report["coverage_policy"] = (
            "Complete only with explicit interval coverage declaration and no invalid/ambiguous rows; "
            "recognized policy exclusions are not missing catalogue data"
        )
        return ingest_records(
            experiment.source_id,
            rows,
            start,
            end,
            report,
            status="complete" if declared and not report["rejected_count"] else "partial",
            discovery_context={"grid": grid, "config": experiment.config, "discovery_interval": (start, end)},
            import_identity={
                "namespace": payload["namespace"],
                "format": payload["format"],
                "artifact": payload["artifact"],
                "line_count": report["line_count"],
                "mapping": report.get("mapping", {}),
                "parser_version": report["parser_version"],
                "coverage_declared": declared,
                "catalogue_namespace": experiment.config.get("catalogue_namespace"),
            },
        )

    experiment_id = payload.get("experiment_id")
    if kind == "reconcile":
        if not experiment_id or not payload.get("start") or not payload.get("end"):
            raise ValueError("Scheduled reconciliation requires an experiment and pinned interval")
        start, end = utc(payload["start"]), utc(payload["end"])
        if end - start != timedelta(days=31) or end > now():
            raise ValueError("Scheduled reconciliation requires a completed 31-day interval")
        experiment, _ = load_experiment(experiment_id)
        if experiment.synthetic or experiment.config["provider"] not in {"usgs", "jma"}:
            raise ValueError("Scheduled reconciliation requires a real USGS or JMA experiment")
        progress(0)
        ingestion = None
        if experiment.config["provider"] == "usgs":
            ingestion = ingest_usgs(experiment_id, start, end, lambda p: progress(0.8 * p), use_feed=False)
        # One durable job provides actual ordering: a failed refresh cannot be
        # followed by its evaluation merely because two queued jobs tied in time.
        # A partial refresh may still yield explicit unavailable observations.
        progress(0.8)
        evaluation = execute("evaluate", {"experiment_id": experiment_id}, lambda p: progress(0.8 + 0.2 * p))
        progress(1)
        return {
            "experiment_id": experiment_id,
            "scheduled_end": end.isoformat(),
            "ingestion": ingestion,
            **evaluation,
        }
    if kind == "ingest":
        experiment_id = experiment_id or real_experiment().id
        end = utc(payload["end"]) if payload.get("end") else now()
        start = utc(payload["start"]) if payload.get("start") else end - timedelta(days=7)
        return {
            "experiment_id": experiment_id,
            **ingest_usgs(experiment_id, start, end, progress, use_feed=(end - start).days <= 7),
        }
    # CLI/durable payloads bypass the HTTP schema. Reject invalid chronology
    # before even the fallback experiment/source/grid can be created.
    if kind == "train":
        timestamp = now()
        cutoff = validate_training_inputs(
            payload["end"] if payload.get("end") else timestamp,
            timestamp,
            payload.get("fit_days", 180),
            payload.get("tune_days", 30),
            payload.get("calibration_days", 30),
        )
    elif kind == "backtest":
        if not payload.get("start"):
            raise ValueError("Backtests require an explicit first test-window start")
        start = validate_backtest_inputs(
            payload["start"],
            now(),
            payload.get("test_days", 7),
            payload.get("folds", 1),
            payload.get("fit_days", 180),
            payload.get("tune_days", 30),
            payload.get("calibration_days", 30),
        )
    if not experiment_id:
        experiment_id = real_experiment().id
    if kind == "train":
        models = train_suite(
            experiment_id,
            cutoff,
            payload.get("fit_days", 180),
            payload.get("tune_days", 30),
            payload.get("calibration_days", 30),
            progress,
        )
        return {
            "experiment_id": experiment_id,
            "models": [{"id": m.id, "algorithm": m.algorithm, "status": m.status} for m in models],
        }
    if kind == "evaluate":
        with transaction() as s:
            ids = list(
                s.scalars(
                    select(Forecast.id)
                    .where(Forecast.experiment_id == experiment_id, Forecast.end <= now())
                    .order_by(Forecast.start)
                )
            )
        result = []
        for index, forecast_id in enumerate(ids):
            e = evaluate_forecast(forecast_id)
            result.append({"id": e.id, "status": e.status})
            progress((index + 1) / len(ids))
        return {"evaluations": result}
    if kind == "backtest":
        return run_backtest(
            experiment_id,
            start,
            payload.get("test_days", 7),
            payload.get("folds", 1),
            payload.get("fit_days", 180),
            payload.get("tune_days", 30),
            payload.get("calibration_days", 30),
            progress,
        )
    raise ValueError(f"Unknown job kind: {kind}")
