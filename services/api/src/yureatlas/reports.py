"""Bounded metric reports built from one coherent, read-only archive snapshot."""

from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import tables as t
from .artifacts import digest
from .comparisons import build_comparison_report
from .db import engine
from .schemas import MetricReport, MetricReportRequest
from .tables import now

MAX_SPAN_DAYS = 3650
MAX_FORECASTS = 2000
MAX_CELL_ROWS = 1_000_000
MAX_GRID_CELLS = 10_000


class ReportNotFound(ValueError):
    pass


class ReportTimeout(RuntimeError):
    pass


@contextmanager
def transaction():
    # Configure isolation before BEGIN. Pool reset restores connection defaults;
    # no session-level setting or write escapes this bounded read transaction.
    with (
        engine.connect().execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        ) as connection,
        connection.begin(),
        Session(bind=connection) as session,
    ):
        session.execute(text("SET LOCAL statement_timeout = '15s'"))
        yield session


def report_content_id(report):
    """Hash the delivered typed content; omit only generation time and this ID itself."""
    return digest({key: value for key, value in report.items() if key not in {"generated_at", "report_id"}})


def _limits():
    return {
        "max_span_days": MAX_SPAN_DAYS,
        "max_forecasts": MAX_FORECASTS,
        "max_cell_rows": MAX_CELL_ROWS,
        "max_grid_cells": MAX_GRID_CELLS,
    }


def _budget_error(name, limit):
    raise ValueError(f"Report exceeds {name} limit ({limit}); choose a narrower report interval")


def _collect(experiment_id, request, generated_at):
    with transaction() as s:
        identity = s.execute(
            select(t.Experiment, t.Grid, t.Source)
            .join(t.Grid, t.Grid.id == t.Experiment.grid_id)
            .join(t.Source, t.Source.id == t.Experiment.source_id)
            .where(t.Experiment.id == experiment_id)
        ).one_or_none()
        if identity is None:
            raise ReportNotFound("Unknown experiment")
        experiment, grid, source = identity
        metadata = {
            "id": experiment.id,
            "name": experiment.name,
            "config_hash": experiment.config_hash,
            "config": experiment.config,
            "grid_id": grid.id,
            "grid_hash": grid.geometry_hash,
            "source_id": source.id,
            "source_name": source.name,
            "source_provider": source.provider,
            "synthetic": experiment.synthetic,
        }
        predicates = [t.Forecast.experiment_id == experiment_id, t.Forecast.mode == request.mode]
        kind = (
            "explicit"
            if request.start is not None
            else "whole_experiment"
            if request.days == 0
            else "relative"
        )
        scope = {
            "kind": kind,
            "requested_days": request.days,
            "requested_start": request.start,
            "requested_end": request.end,
            "resolved_start": request.start,
            "resolved_end": request.end,
            "anchor": None,
            "empty_reason": None,
            "display_timezone": "Asia/Tokyo",
            "containment_policy": "Include full forecast windows: start >= resolved_start and end <= resolved_end; resolved end is exclusive.",
            "limits": _limits(),
        }
        if kind == "relative":
            anchor = s.scalar(
                select(func.max(t.Forecast.end)).where(
                    *predicates,
                    t.Forecast.end <= generated_at,
                    select(t.Evaluation.id).where(t.Evaluation.forecast_id == t.Forecast.id).exists(),
                )
            )
            scope["anchor"] = anchor
            if anchor is None:
                scope["empty_reason"] = "no_completed_evaluated_window"
                return (
                    metadata,
                    scope,
                    [],
                    {
                        "forecast_count": 0,
                        "evaluation_count": 0,
                        "forecast_cell_rows": 0,
                        "evaluation_cell_rows": 0,
                    },
                )
            scope["resolved_start"] = anchor - timedelta(days=request.days)
            scope["resolved_end"] = anchor
        if kind != "whole_experiment":
            predicates.extend(
                [t.Forecast.start >= scope["resolved_start"], t.Forecast.end <= scope["resolved_end"]]
            )
        count, first, last = s.execute(
            select(func.count(t.Forecast.id), func.min(t.Forecast.start), func.max(t.Forecast.end)).where(
                *predicates
            )
        ).one()
        if count > MAX_FORECASTS:
            _budget_error("forecast count", MAX_FORECASTS)
        if kind == "whole_experiment":
            scope.update(resolved_start=first, resolved_end=last)
            if first is not None and last - first > timedelta(days=MAX_SPAN_DAYS):
                _budget_error("whole-experiment span in days", MAX_SPAN_DAYS)
        counts = {
            "forecast_count": count,
            "evaluation_count": 0,
            "forecast_cell_rows": 0,
            "evaluation_cell_rows": 0,
        }
        if not count:
            scope["empty_reason"] = "no_forecasts" if kind == "whole_experiment" else "no_forecasts_in_range"
            return metadata, scope, [], counts
        grid_count = s.scalar(select(func.count()).select_from(t.Cell).where(t.Cell.grid_id == grid.id))
        if grid_count > MAX_GRID_CELLS:
            _budget_error("grid cell count", MAX_GRID_CELLS)
        ranked = (
            select(
                t.Evaluation.id,
                func.row_number()
                .over(
                    partition_by=t.Evaluation.forecast_id,
                    order_by=(
                        t.Evaluation.version.desc(),
                        t.Evaluation.created_at.desc(),
                        t.Evaluation.id.desc(),
                    ),
                )
                .label("position"),
            )
            .join(t.Forecast, t.Forecast.id == t.Evaluation.forecast_id)
            .where(*predicates)
            .subquery()
        )
        counts["forecast_cell_rows"] = s.scalar(
            select(func.count())
            .select_from(t.ForecastCell)
            .join(t.Forecast, t.Forecast.id == t.ForecastCell.forecast_id)
            .where(*predicates)
        )
        counts["evaluation_cell_rows"] = s.scalar(
            select(func.count())
            .select_from(t.EvaluationCell)
            .join(ranked, ranked.c.id == t.EvaluationCell.evaluation_id)
            .where(ranked.c.position == 1)
        )
        if counts["forecast_cell_rows"] + counts["evaluation_cell_rows"] > MAX_CELL_ROWS:
            _budget_error("forecast/evaluation cell evidence rows", MAX_CELL_ROWS)
        forecasts = list(
            s.execute(
                select(t.Forecast, t.Model)
                .join(t.Model, t.Model.id == t.Forecast.model_id)
                .where(*predicates)
            )
        )
        grid_ids = set(s.scalars(select(t.Cell.id).where(t.Cell.grid_id == grid.id)))
        evaluated = {
            evaluation.forecast_id: (evaluation, snapshot)
            for evaluation, snapshot in s.execute(
                select(t.Evaluation, t.Snapshot)
                .join(ranked, ranked.c.id == t.Evaluation.id)
                .outerjoin(t.Snapshot, t.Snapshot.id == t.Evaluation.snapshot_id)
                .where(ranked.c.position == 1)
            )
        }
        counts["evaluation_count"] = len(evaluated)
        observations, forecast_cells = defaultdict(list), defaultdict(set)
        evaluation_ids = [evaluation.id for evaluation, _ in evaluated.values()]
        for offset in range(0, len(evaluation_ids), 1000):
            for evaluation_id, cell_id, observed in s.execute(
                select(
                    t.EvaluationCell.evaluation_id, t.EvaluationCell.cell_id, t.EvaluationCell.observed_count
                ).where(t.EvaluationCell.evaluation_id.in_(evaluation_ids[offset : offset + 1000]))
            ):
                observations[evaluation_id].append((cell_id, int(observed > 0) if observed >= 0 else -1))
        for forecast_id, cell_id in s.execute(
            select(t.ForecastCell.forecast_id, t.ForecastCell.cell_id)
            .join(t.Forecast, t.Forecast.id == t.ForecastCell.forecast_id)
            .where(*predicates)
        ):
            forecast_cells[forecast_id].add(cell_id)
        records = []
        for forecast, model in forecasts:
            evaluation, snapshot = evaluated.get(forecast.id, (None, None))
            records.append(
                {
                    "forecast_id": forecast.id,
                    "forecast_created_at": forecast.created_at,
                    "model_id": model.id,
                    "algorithm": model.algorithm,
                    "experiment_id": forecast.experiment_id,
                    "start": forecast.start,
                    "end": forecast.end,
                    "mode": forecast.mode,
                    "grid_hash": forecast.provenance.get("grid_hash"),
                    "grid_cell_ids": grid_ids,
                    "forecast_cell_ids": forecast_cells[forecast.id],
                    "evidence_valid": (
                        forecast.provenance.get("grid_hash") == grid.geometry_hash
                        and model.experiment_id == forecast.experiment_id
                        and snapshot is not None
                        and snapshot.source_id == source.id
                    ),
                    "evaluation_id": evaluation.id if evaluation else None,
                    "evaluation_version": evaluation.version if evaluation else None,
                    "status": evaluation.status if evaluation else "not_evaluated",
                    "revision_ids": snapshot.revision_ids if snapshot else None,
                    "observations": observations[evaluation.id] if evaluation else None,
                    "metrics": evaluation.metrics if evaluation else None,
                }
            )
        return metadata, scope, records, counts


def build_metric_report(experiment_id, mode, *, days=None, start=None, end=None):
    request = MetricReportRequest(mode=mode, days=days, start=start, end=end)
    generated_at = now()
    try:
        metadata, scope, records, counts = _collect(experiment_id, request, generated_at)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "57014":
            raise ReportTimeout(
                "Report query timed out; choose a narrower report interval and retry"
            ) from error
        raise
    # Release the DB snapshot before resampling, schema normalization and hashing.
    comparison = build_comparison_report(records)
    for model in comparison["comparisons"]:
        model.update(paired_forecasts=model["paired_days"], excluded_forecasts=model["excluded_days"])
    body = MetricReport.model_validate(
        {
            "report_id": "0" * 64,
            "generated_at": generated_at,
            "mode": request.mode,
            "days": request.days,
            "scope": scope,
            "experiment": metadata,
            "policies": {
                "generation": "One PostgreSQL REPEATABLE READ, READ ONLY snapshot; generated_at is generation metadata, not a catalogue receipt cutoff.",
                "evaluation": "Latest archived evaluation by version, created_at, ID within the database snapshot; immutable selected evaluation IDs are retained.",
                "inclusion": "All full forecast windows contained in the resolved scope, including unevaluated and unavailable windows in decisions and denominators.",
            },
            "counts": counts,
            "selected_forecast_ids": sorted(record["forecast_id"] for record in records),
            "selected_evaluation_ids": sorted(
                record["evaluation_id"] for record in records if record["evaluation_id"] is not None
            ),
            **comparison,
        }
    ).model_dump(mode="json")
    body["report_id"] = report_content_id(body)
    return body
