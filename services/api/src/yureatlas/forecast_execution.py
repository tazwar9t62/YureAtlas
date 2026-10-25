"""Pin each durable forecast once; retries reuse its original publication identity."""

from datetime import datetime, timedelta

from pydantic import TypeAdapter
from sqlalchemy import select, text

from .archive import issue_forecast
from .artifacts import digest
from .db import Session, transaction
from .execution import current_execution, verify_attempt
from .forecast_errors import LegacyRetryIdentityUnknown, PublicationDeadlineMissed
from .forecast_execution_schema import ForecastExecution, ForecastTarget, ResolvedForecastExecution
from .ingestion import real_experiment
from .tables import Experiment, Forecast, Model, Promotion, now
from .timeutil import utc

EXECUTION_SCHEMA = TypeAdapter(ForecastExecution)
SELECTION_POLICY = "forecast-selection-v1"


def _instant(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("Forecast times must be aware timestamps")  # noqa: TRY004 - request validation
    return utc(value)


def pin_forecast_target(payload, requested_at) -> dict:
    """Capture the promised target without rewriting the original request."""
    if not isinstance(payload, dict):
        raise ValueError("Forecast request must be an object")  # noqa: TRY004 - request validation
    requested_at = _instant(requested_at)
    explicit = payload.get("start") is not None
    if payload.get("scheduled") and not explicit:
        raise ValueError("Scheduled forecasts require an explicit target start")
    if payload.get("mode", "prospective") != "prospective":
        raise ValueError("Forecast jobs require prospective mode; use backtests for historical windows")
    try:
        start = _instant(payload["start"]) if explicit else requested_at + timedelta(minutes=30)
        return ForecastTarget(
            version=1,
            state="target_pinned",
            start=start,
            end=start + timedelta(days=1),
            input_cutoff=start - timedelta(minutes=30),
            mode="prospective",
            target_basis="explicit_start" if explicit else "request_time",
            request_time=requested_at,
        ).model_dump(mode="json")
    except OverflowError as error:
        raise ValueError("Forecast target lies outside the supported timestamp range") from error


def database_now(session):
    """Read the live database clock after acquiring ownership row locks."""
    return session.scalar(text("SELECT clock_timestamp()"))


def _existing(session, experiment_id, model_id, target):
    return session.scalar(
        select(Forecast)
        .where(
            Forecast.experiment_id == experiment_id,
            Forecast.model_id == model_id,
            Forecast.start == target.start,
            Forecast.end == target.end,
            Forecast.input_cutoff == target.input_cutoff,
            Forecast.mode == target.mode,
            Forecast.status == "published",
        )
        .order_by(Forecast.created_at, Forecast.id)
        .limit(1)
    )


def _deadline(target, checked_at):
    if checked_at >= target.start:
        raise PublicationDeadlineMissed(target.start, "before model selection", checked_at)


def _model(session, experiment_id, model_id):
    model = session.get(Model, model_id)
    if model is None or model.experiment_id != experiment_id:
        raise ValueError("Model and experiment are incompatible")
    return model


def _validate_model(model, cutoff, checked_at):
    if model.status != "available" or not model.artifact:
        raise ValueError("Selected forecast model is unavailable")
    if model.training_cutoff > cutoff:
        raise ValueError("Selected model training cutoff is after the forecast input cutoff")
    if model.available_at > checked_at:
        raise ValueError("Selected model artifact is not yet available")


def _selection(model, kind, checked_at, promotion=None):
    return {
        "policy": SELECTION_POLICY,
        "kind": kind,
        "selected_at": checked_at.isoformat(),
        "promotion_id": promotion.id if promotion is not None else None,
        "promotion_created_at": promotion.created_at.isoformat() if promotion is not None else None,
        "promotion_reason": promotion.reason if promotion is not None else None,
        "model_artifact": model.artifact,
        "model_training_cutoff": model.training_cutoff.isoformat(),
        "model_available_at": model.available_at.isoformat(),
    }


def select_forecast_model(session, experiment_id, requested_model_id, cutoff, checked_at):
    """Read-only selection: requested model, latest promotion, then latest rate.

    A selected incompatible promotion is an error, not permission to silently
    use a different model. IDs break equal archive-time ties deterministically.
    """
    experiment = session.get(Experiment, experiment_id)
    if experiment is None:
        raise ValueError("Unknown forecast experiment")
    if experiment.synthetic:
        raise ValueError("Synthetic and real forecasts must remain separate")
    promotion = None
    if requested_model_id is not None:
        if not isinstance(requested_model_id, str) or not requested_model_id:
            raise ValueError("Forecast model ID must be a nonempty string")
        model = _model(session, experiment_id, requested_model_id)
        kind = "requested"
    else:
        promotion = session.scalar(
            select(Promotion)
            .where(Promotion.experiment_id == experiment_id, Promotion.created_at <= checked_at)
            .order_by(Promotion.created_at.desc(), Promotion.id.desc())
            .limit(1)
        )
        if promotion is not None:
            model = _model(session, experiment_id, promotion.model_id)
            kind = "promotion"
        else:
            model = session.scalar(
                select(Model)
                .where(
                    Model.experiment_id == experiment_id,
                    Model.status == "available",
                    Model.algorithm == "historical_rate",
                    Model.created_at <= checked_at,
                )
                .order_by(Model.created_at.desc(), Model.id.desc())
                .limit(1)
            )
            if model is None:
                raise ValueError("No eligible model. Import sufficient history and train first")
            kind = "historical_rate"
    _validate_model(model, cutoff, checked_at)
    return model, _selection(model, kind, checked_at, promotion)


def _complete(raw, experiment_id, model, selection):
    target = ForecastTarget.model_validate(raw)
    result = {
        **raw,
        "state": "resolved",
        "experiment_id": experiment_id,
        "model_id": model.id,
        "forecast_key": digest(
            {"experiment": experiment_id, "model": model.id, "start": target.start, "mode": target.mode}
        ),
        "selection": selection,
    }
    ResolvedForecastExecution.model_validate(result)
    return result


def _validate_request_binding(raw, payload, requested_at):
    target = EXECUTION_SCHEMA.validate_python(raw)
    expected = ForecastTarget.model_validate(pin_forecast_target(payload, requested_at))
    for name in ("start", "end", "input_cutoff", "mode", "request_time"):
        if getattr(target, name) != getattr(expected, name):
            raise ValueError("Stored forecast execution conflicts with its original request")
    if isinstance(target, ResolvedForecastExecution):
        for name in ("experiment_id", "model_id"):
            if payload.get(name) is not None and payload[name] != getattr(target, name):
                raise ValueError("Resolved forecast identity conflicts with its original request")
    return target


def _resolve_in_session(session, payload, raw, checked_at, default_experiment_id=None):
    """Return a complete record or None when default experiment creation is needed."""
    target = EXECUTION_SCHEMA.validate_python(raw)
    if isinstance(target, ResolvedForecastExecution):
        # Publication is immutable evidence; retry does not load its artifact or
        # substitute today's promoted model, even after the deadline.
        if _existing(session, target.experiment_id, target.model_id, target):
            return raw
        _deadline(target, checked_at)
        model = _model(session, target.experiment_id, target.model_id)
        _validate_model(model, target.input_cutoff, checked_at)
        return raw

    experiment_id = payload.get("experiment_id") or default_experiment_id
    requested_model = payload.get("model_id")
    if experiment_id and requested_model:
        existing = _existing(session, experiment_id, requested_model, target)
        if existing is not None:
            model = _model(session, experiment_id, requested_model)
            return _complete(raw, experiment_id, model, _selection(model, "requested", checked_at))
    _deadline(target, checked_at)
    if checked_at < target.input_cutoff:
        raise ValueError("Future input cutoff; retry at or after the declared cutoff")
    if experiment_id is None:
        return None
    if not isinstance(experiment_id, str) or not experiment_id:
        raise ValueError("Forecast experiment ID must be a nonempty string")
    model, selection = select_forecast_model(
        session, experiment_id, requested_model, target.input_cutoff, checked_at
    )
    return _complete(raw, experiment_id, model, selection)


def _resolve_durable(context):
    default_experiment_id = None
    while True:
        with Session.begin() as session:
            job = verify_attempt(session, context.job_id, context.attempt_token, context.owner)
            if job.kind != "forecast" or not isinstance(job.payload, dict):
                raise ValueError("Forecast resolution requires a forecast job")
            payload = job.payload
            if job.execution is None:
                explicit_identity = all(
                    isinstance(payload.get(key), str) and payload[key]
                    for key in ("experiment_id", "start", "model_id")
                )
                if job.attempts > 1 and not explicit_identity:
                    raise LegacyRetryIdentityUnknown(job.id)
                raw = pin_forecast_target(payload, job.created_at)
                raw["target_basis"] = (
                    "legacy_explicit_start" if payload.get("start") is not None else "legacy_request_time"
                )
            else:
                raw = dict(job.execution)
            _validate_request_binding(raw, payload, job.created_at)
            result = _resolve_in_session(session, payload, raw, database_now(session), default_experiment_id)
            job.execution = result if result is not None else raw
            if result is not None:
                return result
        # This may acquire experiment/source/grid locks. It must happen after
        # releasing all worker/job locks; the next loop verifies ownership anew.
        default_experiment_id = real_experiment().id


def resolve_forecast(payload) -> dict:
    """Persist a complete durable resolution, or resolve an explicit inline call."""
    context = current_execution()
    if context is not None:
        if context.job_id is None:
            raise ValueError("Durable forecast execution requires a claimed job attempt")
        return _resolve_durable(context)

    requested_at = now()
    raw = pin_forecast_target(payload, requested_at)
    if raw["target_basis"] == "request_time":
        raw["target_basis"] = "inline_request_time"
    default_experiment_id = None
    while True:
        with transaction() as session:
            result = _resolve_in_session(session, payload, raw, now(), default_experiment_id)
            if result is not None:
                return result
        default_experiment_id = real_experiment().id


def run_forecast(payload, progress):
    resolution = ResolvedForecastExecution.model_validate(resolve_forecast(payload))
    forecast = issue_forecast(
        resolution.experiment_id,
        resolution.model_id,
        resolution.start,
        cutoff=resolution.input_cutoff,
        mode=resolution.mode,
        progress=progress,
    )
    return {"forecast_id": forecast.id}


def authorize_forecast(experiment_id, model_id, start, cutoff, mode):
    """Reject bypassing a claimed forecast job's immutable complete resolution.

    Backtest jobs intentionally issue a series of forecasts. Direct inline
    callers have no durable job identity; their archive validation is unchanged.
    This control transaction finishes before archive/domain locks are acquired.
    """
    context = current_execution()
    if context is None or context.job_id is None:
        return
    with Session.begin() as session:
        job = verify_attempt(session, context.job_id, context.attempt_token, context.owner)
        if job.kind != "forecast":
            return
        try:
            resolution = _validate_request_binding(job.execution, job.payload, job.created_at)
            if not isinstance(resolution, ResolvedForecastExecution):
                raise ValueError("Incomplete forecast resolution")  # noqa: TRY004 - a valid but partial stage
        except (TypeError, ValueError) as error:
            raise ValueError("Forecast execution must be resolved before publication") from error
        if (
            resolution.experiment_id != experiment_id
            or resolution.model_id != model_id
            or resolution.start != utc(start)
            or resolution.input_cutoff != utc(cutoff)
            or resolution.mode != mode
        ):
            raise ValueError("Forecast publication does not match its resolved execution")
