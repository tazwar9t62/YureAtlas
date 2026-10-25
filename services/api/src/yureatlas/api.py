import json
from datetime import timedelta

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import tables as t
from .config import settings
from .db import engine, transaction
from .eligibility import eligible_cell
from .schemas import (
    EvaluationView,
    ExperimentSpec,
    ExperimentView,
    ForecastMode,
    ForecastView,
    JobRequest,
    JobView,
    MetricReport,
    ModelView,
)

app = FastAPI(title="YureAtlas", version="0.1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "api", "testserver"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins.split(","):
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site mutation rejected"}, status_code=403)
        if request.headers.get("x-yureatlas-request") != "1":
            return JSONResponse({"detail": "X-YureAtlas-Request: 1 is required"}, status_code=403)
    return await call_next(request)


class Health(BaseModel):
    status: str
    service: str = "YureAtlas"


@app.get("/health", response_model=Health)
def health():
    return Health(status="ok")


@app.get("/ready", response_model=Health)
def ready():
    with engine.connect() as conn:
        conn.execute(text("SELECT version_num FROM alembic_version"))
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    return Health(status="ready")


@app.exception_handler(ValueError)
async def invalid_input(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


def view(row, schema):
    if row is None:
        raise HTTPException(404, "Not found")
    values = {k: getattr(row, "metadata_" if k == "metadata" else k) for k in schema.model_fields}
    return schema(**values)


@app.get("/experiments", response_model=list[ExperimentView])
def experiments():
    with transaction() as s:
        return [
            view(e, ExperimentView) for e in s.scalars(select(t.Experiment).order_by(t.Experiment.created_at))
        ]


@app.post("/experiments", response_model=ExperimentView, status_code=201)
def create_experiment(spec: ExperimentSpec):
    from .catalogue import ensure_experiment

    return view(ensure_experiment(spec), ExperimentView)


@app.get("/models", response_model=list[ModelView])
def models(experiment_id: str):
    with transaction() as s:
        return [
            view(m, ModelView)
            for m in s.scalars(
                select(t.Model)
                .where(t.Model.experiment_id == experiment_id)
                .order_by(t.Model.created_at.desc())
            )
        ]


@app.get("/forecasts", response_model=list[ForecastView])
def forecasts(
    experiment_id: str,
    model_id: str | None = None,
    mode: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
):
    with transaction() as s:
        query = select(t.Forecast).where(t.Forecast.experiment_id == experiment_id)
        if model_id:
            query = query.where(t.Forecast.model_id == model_id)
        if mode:
            query = query.where(t.Forecast.mode == mode)
        return [
            view(f, ForecastView)
            for f in s.scalars(query.order_by(t.Forecast.start.desc(), t.Forecast.created_at).limit(limit))
        ]


@app.get("/forecasts/{forecast_id}", response_model=ForecastView)
def forecast_detail(forecast_id: str):
    with transaction() as s:
        return view(s.get(t.Forecast, forecast_id), ForecastView)


def latest_evaluation(s, forecast_id):
    return s.scalar(
        select(t.Evaluation)
        .where(t.Evaluation.forecast_id == forecast_id)
        .order_by(t.Evaluation.version.desc())
        .limit(1)
    )


@app.get("/forecasts/{forecast_id}/map")
def forecast_map(forecast_id: str) -> dict:
    with transaction() as s:
        forecast = s.get(t.Forecast, forecast_id)
        if not forecast:
            raise HTTPException(404, "Unknown forecast")
        experiment = s.get(t.Experiment, forecast.experiment_id)
        query = (
            select(t.Cell, t.ForecastCell)
            .join(t.ForecastCell, t.ForecastCell.cell_id == t.Cell.id)
            .where(t.ForecastCell.forecast_id == forecast_id, t.Cell.grid_id == experiment.grid_id)
        )
        evaluation = latest_evaluation(s, forecast_id)
        observed = (
            {}
            if not evaluation
            else {
                c.cell_id: c
                for c in s.scalars(
                    select(t.EvaluationCell).where(t.EvaluationCell.evaluation_id == evaluation.id)
                )
            }
        )
        features = [
            {
                "type": "Feature",
                "id": c.id,
                "geometry": c.geojson,
                "properties": {
                    "cell_id": c.id,
                    "probability": f.probability,
                    "band": f.band,
                    "area_km2": c.area_km2,
                    "observed_count": observed[c.id].observed_count if c.id in observed else None,
                    "outcome": observed[c.id].outcome if c.id in observed else "unknown",
                },
            }
            for c, f in s.execute(query)
        ]
        event_features = []
        if evaluation and evaluation.status != "awaiting_data":
            snapshot = s.get(t.Snapshot, evaluation.snapshot_id)
            rows = s.scalars(
                select(t.Revision).where(
                    t.Revision.id.in_(snapshot.revision_ids),
                    t.Revision.origin_time >= forecast.start,
                    t.Revision.origin_time < forecast.end,
                    t.Revision.magnitude >= experiment.config["target_magnitude"],
                )
            )
            from .grid import StudyGrid

            grid = StudyGrid.from_spec(s.get(t.Grid, experiment.grid_id).spec)
            event_features = [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r.longitude, r.latitude]},
                    "properties": {
                        "id": r.event_id,
                        "magnitude": r.magnitude,
                        "magnitude_type": r.magnitude_type,
                        "depth_km": r.depth_km,
                        "time": r.origin_time.isoformat(),
                    },
                }
                for r in rows
                if eligible_cell(r, grid, experiment.config, minimum=experiment.config["target_magnitude"])
                is not None
            ]
        return {
            "cells": {"type": "FeatureCollection", "features": features},
            "events": {"type": "FeatureCollection", "features": event_features},
            "evaluation": view(evaluation, EvaluationView) if evaluation else None,
        }


@app.get("/forecasts/{forecast_id}/cells/{cell_id}")
def cell_detail(forecast_id: str, cell_id: str) -> dict:
    with transaction() as s:
        forecast = s.get(t.Forecast, forecast_id)
        cell = s.get(t.ForecastCell, (forecast_id, cell_id))
        if not forecast or not cell:
            raise HTTPException(404, "Unknown forecast/cell")
        experiment = s.get(t.Experiment, forecast.experiment_id)
        model = s.get(t.Model, forecast.model_id)
        evaluation = latest_evaluation(s, forecast_id)
        outcome = s.get(t.EvaluationCell, (evaluation.id, cell_id)) if evaluation else None
        prior = s.execute(
            select(t.Forecast.start, t.ForecastCell.probability)
            .join(t.ForecastCell, t.ForecastCell.forecast_id == t.Forecast.id)
            .where(
                t.Forecast.experiment_id == experiment.id,
                t.Forecast.model_id == model.id,
                t.Forecast.mode == forecast.mode,
                t.Forecast.start < forecast.start,
                t.ForecastCell.cell_id == cell_id,
            )
            .order_by(t.Forecast.start.desc())
            .limit(7)
        ).all()
        return {
            "cell_id": cell_id,
            "probability": cell.probability,
            "band": cell.band,
            "expected_count": cell.expected_count,
            "target": f"At least one catalogue event M ≥ {experiment.config['target_magnitude']} in 24 hours",
            "source": experiment.config["magnitude_policy"],
            "model": model.algorithm,
            "model_id": model.id,
            "model_artifact": model.artifact,
            "input_cutoff": forecast.input_cutoff,
            "observed_count": outcome.observed_count if outcome else None,
            "outcome": outcome.outcome if outcome else "unknown",
            "history": [{"start": a, "probability": p} for a, p in prior],
        }


@app.get("/evaluations/{evaluation_id}", response_model=EvaluationView)
def evaluation_detail(evaluation_id: str):
    with transaction() as s:
        return view(s.get(t.Evaluation, evaluation_id), EvaluationView)


@app.get("/forecasts/{forecast_id}/evaluations", response_model=list[EvaluationView])
def evaluation_history(forecast_id: str):
    with transaction() as s:
        return [
            view(e, EvaluationView)
            for e in s.scalars(
                select(t.Evaluation)
                .where(t.Evaluation.forecast_id == forecast_id)
                .order_by(t.Evaluation.version.desc())
            )
        ]


@app.get("/jobs", response_model=list[JobView])
def jobs(limit: int = Query(30, ge=1, le=100)):
    with transaction() as s:
        return [
            view(j, JobView) for j in s.scalars(select(t.Job).order_by(t.Job.created_at.desc()).limit(limit))
        ]


@app.get("/jobs/{job_id}", response_model=JobView)
def job_detail(job_id: str):
    with transaction() as s:
        return view(s.get(t.Job, job_id), JobView)


@app.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str):
    from .jobs import request_cancel

    job = request_cancel(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return view(job, JobView)


def submit(kind: str, body: JobRequest):
    from .jobs import enqueue

    if body.experiment_id:
        with transaction() as s:
            if not s.get(t.Experiment, body.experiment_id):
                raise HTTPException(404, "Unknown experiment")
    return view(enqueue(kind, body.model_dump(mode="json"), f"{kind}:{body.key or t.uid()}"), JobView)


@app.post("/demo", response_model=JobView, status_code=202)
def demo():
    return submit("demo", JobRequest(key="synthetic-seed-v1"))


@app.post("/ingestions", response_model=JobView, status_code=202)
def ingestion(body: JobRequest):
    return submit("ingest", body)


@app.post("/training-runs", response_model=JobView, status_code=202)
def training(body: JobRequest):
    return submit("train", body)


@app.post("/forecasts", response_model=JobView, status_code=202)
def forecast_create(body: JobRequest):
    return submit("forecast", body)


@app.post("/evaluations", response_model=JobView, status_code=202)
def evaluation_create(body: JobRequest):
    return submit("evaluate", body)


@app.post("/backtests", response_model=JobView, status_code=202)
def backtest(body: JobRequest):
    return submit("backtest", body)


class PromotionRequest(BaseModel):
    model_id: str
    reason: str = Field(min_length=5, max_length=500)


@app.post("/model-promotions")
def promote(body: PromotionRequest) -> dict:
    with transaction() as s:
        model = s.get(t.Model, body.model_id)
        if not model or model.status != "available":
            raise HTTPException(422, "Only an available compatible model can be promoted")
        promotion = t.Promotion(experiment_id=model.experiment_id, model_id=model.id, reason=body.reason)
        s.add(promotion)
        s.flush()
        return {"id": promotion.id, "model_id": model.id, "affects": "future forecasts only"}


@app.get("/sources")
def sources() -> dict:
    with transaction() as s:
        result = []
        for source in s.scalars(select(t.Source).order_by(t.Source.created_at)):
            counts = s.scalar(select(func.count()).select_from(t.Event).where(t.Event.source_id == source.id))
            coverage = s.execute(
                select(t.Ingestion.start, t.Ingestion.end, t.Ingestion.status, t.Ingestion.report)
                .where(t.Ingestion.source_id == source.id)
                .order_by(t.Ingestion.created_at.desc())
                .limit(40)
            ).all()
            result.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "provider": source.provider,
                    "status": source.status,
                    "attribution": source.attribution,
                    "event_count": counts,
                    "details": source.details,
                    "coverage": [{"start": a, "end": b, "status": c, "report": d} for a, b, c, d in coverage],
                }
            )
        worker = s.get(t.WorkerState, "scheduler")
        return {
            "sources": result,
            "worker": {
                "heartbeat": worker.heartbeat,
                "online": worker.heartbeat > t.now() - timedelta(seconds=90),
            }
            if worker
            else None,
            "schedule_timezone": "Asia/Tokyo",
            "schedule_enabled": settings.schedule_enabled,
        }


@app.get("/events")
def events(
    experiment_id: str,
    start: str | None = None,
    end: str | None = None,
    min_magnitude: float = Query(0, ge=-3, le=10),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    from .catalogue import load_experiment, revisions_asof
    from .timeutil import utc

    experiment, grid = load_experiment(experiment_id)
    rows = [
        r
        for r in revisions_asof(experiment.source_id, t.now())
        if eligible_cell(r, grid, experiment.config, minimum=min_magnitude) is not None
        and (not start or r.origin_time >= utc(start))
        and (not end or r.origin_time < utc(end))
    ]
    return {
        "total": len(rows),
        "offset": offset,
        "items": [revision_view(r) for r in rows[offset : offset + limit]],
    }


def revision_view(r):
    return {
        k: getattr(r, k)
        for k in (
            "id",
            "event_id",
            "origin_time",
            "received_at",
            "provider_updated",
            "longitude",
            "latitude",
            "depth_km",
            "magnitude",
            "magnitude_type",
            "status",
            "event_type",
            "selection_reason",
            "hash_parent_id",
            "raw_ref",
        )
    }


@app.get("/events/{event_id}/revisions")
def event_revisions(event_id: str) -> list[dict]:
    with transaction() as s:
        return [
            revision_view(r)
            for r in s.scalars(
                select(t.Revision).where(t.Revision.event_id == event_id).order_by(t.Revision.received_at)
            )
        ]


def _metric_report(experiment_id, mode, days, start, end):
    from .reports import ReportNotFound, ReportTimeout, build_metric_report

    try:
        return build_metric_report(experiment_id, mode, days=days, start=start, end=end)
    except ReportNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ReportTimeout as error:
        raise HTTPException(503, str(error)) from error


@app.get("/metrics", response_model=MetricReport)
def metrics(
    experiment_id: str,
    mode: ForecastMode,
    days: int | None = Query(None, ge=0, le=3650),
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
) -> dict:
    return _metric_report(experiment_id, mode, days, start, end)


def unique_mapping_object(pairs):
    if len(dict(pairs)) != len(pairs):
        raise ValueError("CSV mapping contains duplicate source column names")
    return dict(pairs)


@app.post("/imports", response_model=JobView, status_code=202)
async def import_file(
    experiment_id: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    namespace: str = Form(..., min_length=1, max_length=80),
    format: str = Form("jma"),
    mapping: str = Form("{}", max_length=8192),
    coverage_declared: bool = Form(False),
    submission_id: str = Form("", max_length=80, pattern=r"^[A-Za-z0-9_-]*$"),
    file: UploadFile = File(...),
):
    from .artifacts import digest, put
    from .jobs import enqueue
    from .providers import IMPORT_PARSER_VERSION, validate_csv_mapping
    from .timeutil import utc

    if format not in {"jma", "csv"} or not namespace.strip() or namespace != namespace.strip():
        raise HTTPException(422, "Use jma/csv format and a short stable file namespace")
    columns = validate_csv_mapping(json.loads(mapping, object_pairs_hook=unique_mapping_object))
    if format != "csv" and columns:
        raise HTTPException(422, "Column mapping is only supported for CSV imports")
    a, b = utc(start), utc(end)
    if not a < b <= t.now() or b - a > timedelta(days=366 * 30):
        raise HTTPException(422, "Select a completed catalogue interval of at most 30 years")
    with transaction() as s:
        experiment = s.get(t.Experiment, experiment_id)
        if not experiment or experiment.config["provider"] != "jma":
            raise HTTPException(422, "Select a separate JMA experiment first")
        if not experiment.config.get("catalogue_namespace"):
            raise HTTPException(
                422, "Choose a JMA catalogue collection before importing; legacy archives remain readable"
            )
    raw = await file.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise HTTPException(413, "Maximum uncompressed file size is 32 MiB")
    if not raw:
        raise HTTPException(422, "Upload a nonempty catalogue file")
    ref = put(raw, "txt")
    payload = {
        "experiment_id": experiment_id,
        "start": a.isoformat(),
        "end": b.isoformat(),
        "namespace": namespace,
        "format": format,
        "artifact": ref,
        "mapping": columns,
        "parser_version": f"{format}-v{IMPORT_PARSER_VERSION}",
        "coverage_declared": coverage_declared,
        "submission_id": submission_id,
    }
    # Identical requests (including an optional retry token) share one job. A new
    # token permits an explicit retry of a failed job; the import manifest still
    # deduplicates a previously committed file and declaration.
    key = "import:v2:" + digest(
        {
            "source_id": experiment.source_id,
            "experiment_config_hash": experiment.config_hash,
            "request": payload,
        }
    )
    return view(enqueue("import", payload, key), JobView)


@app.get("/exports/forecasts/{forecast_id}")
def export_forecast(forecast_id: str):
    forecast = forecast_detail(forecast_id)
    body = {"forecast": forecast.model_dump(mode="json"), "map": forecast_map(forecast_id)}
    return JSONResponse(
        __import__("fastapi.encoders", fromlist=["jsonable_encoder"]).jsonable_encoder(body),
        headers={"Content-Disposition": f'attachment; filename="yureatlas-{forecast_id}.json"'},
    )


@app.get("/exports/metrics", response_model=MetricReport)
def export_metrics(
    experiment_id: str,
    mode: ForecastMode,
    days: int | None = Query(None, ge=0, le=3650),
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    expected_report_id: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
):
    report = _metric_report(experiment_id, mode, days, start, end)
    if expected_report_id is not None and expected_report_id != report["report_id"]:
        raise HTTPException(
            409,
            {
                "message": "Report content changed; refresh the report or download the already loaded JSON",
                "expected_report_id": expected_report_id,
                "current_report_id": report["report_id"],
            },
        )
    return JSONResponse(
        report,
        headers={
            "Content-Disposition": f'attachment; filename="yureatlas-metrics-{report["report_id"][:12]}.json"'
        },
    )
