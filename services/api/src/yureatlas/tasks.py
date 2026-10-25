from datetime import timedelta

from sqlalchemy import select

from .archive import evaluate_forecast, issue_forecast
from .catalogue import discovery_and_corrections, ingest_records, load_experiment
from .db import transaction
from .demo import DEMO_TEST_START, seed_catalogue
from .ingestion import ingest_usgs, real_experiment
from .learning import train_suite
from .tables import Forecast, Model, Promotion, now
from .timeutil import utc


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
    experiment, _ = load_experiment(experiment_id)
    if start + timedelta(days=test_days * folds) > now():
        raise ValueError("Backtest target windows must be completed")
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
    if kind == "demo":
        experiment = seed_catalogue(lambda p: progress(p * 0.3))
        return run_backtest(
            experiment.id, DEMO_TEST_START, test_days=7, progress=lambda p: progress(0.3 + p * 0.7)
        )
    if kind == "import":
        from .artifacts import read
        from .providers import parse_csv, parse_jma

        experiment, grid = load_experiment(payload["experiment_id"])
        if experiment.config["provider"] != "jma":
            raise ValueError("Local imports currently require a separate JMA experiment")
        raw = read(payload["artifact"])
        rows, report = (
            parse_jma(raw, payload["namespace"])
            if payload["format"] == "jma"
            else parse_csv(raw, payload.get("mapping"))
        )
        from .eligibility import eligible_cell

        report["selected_in_domain"] = sum(
            eligible_cell(r, grid, experiment.config) is not None for r in rows
        )
        rows = discovery_and_corrections(experiment.source_id, rows, grid, experiment.config)
        report["persisted_candidates"] = len(rows)
        report["raw_ref"] = payload["artifact"]
        report["coverage_policy"] = (
            "User-declared official file interval; rejected rows prevent negative labels"
        )
        return ingest_records(
            experiment.source_id,
            rows,
            utc(payload["start"]),
            utc(payload["end"]),
            report,
            status="complete" if not report["rejected_count"] else "partial",
        )
    experiment_id = payload.get("experiment_id")
    if kind == "ingest":
        experiment_id = experiment_id or real_experiment().id
        end = utc(payload["end"]) if payload.get("end") else now()
        start = utc(payload["start"]) if payload.get("start") else end - timedelta(days=7)
        return {
            "experiment_id": experiment_id,
            **ingest_usgs(experiment_id, start, end, progress, use_feed=(end - start).days <= 7),
        }
    if not experiment_id:
        experiment_id = real_experiment().id
    if kind == "train":
        cutoff = utc(payload["end"]) if payload.get("end") else now()
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
    if kind == "forecast":
        start = utc(payload["start"]) if payload.get("start") else now() + timedelta(minutes=30)
        cutoff = start - timedelta(minutes=30)
        model_id = payload.get("model_id")
        with transaction() as s:
            if not model_id:
                promotion = s.scalar(
                    select(Promotion)
                    .where(Promotion.experiment_id == experiment_id)
                    .order_by(Promotion.created_at.desc())
                    .limit(1)
                )
                model_id = (
                    promotion.model_id
                    if promotion
                    else s.scalar(
                        select(Model.id)
                        .where(
                            Model.experiment_id == experiment_id,
                            Model.status == "available",
                            Model.algorithm == "historical_rate",
                        )
                        .order_by(Model.created_at.desc())
                        .limit(1)
                    )
                )
        if not model_id:
            raise ValueError("No eligible model. Import sufficient history and train first")
        prediction = issue_forecast(experiment_id, model_id, start, cutoff=cutoff, progress=progress)
        return {"forecast_id": prediction.id}
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
        if not payload.get("start"):
            raise ValueError("Backtests require an explicit first test-window start")
        return run_backtest(
            experiment_id,
            utc(payload["start"]),
            payload.get("test_days", 7),
            payload.get("folds", 1),
            payload.get("fit_days", 180),
            payload.get("tune_days", 30),
            payload.get("calibration_days", 30),
            progress,
        )
    raise ValueError(f"Unknown job kind: {kind}")
