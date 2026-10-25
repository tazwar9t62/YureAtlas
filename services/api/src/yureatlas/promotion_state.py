"""Bounded promotion audit; selection delegates to the actual forecast policy.

The selection preview validates archived model metadata. Artifact integrity,
catalogue coverage and publication deadlines remain execution/readiness checks.
"""

import base64
import binascii
import json
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, text

from . import forecast_execution
from .db import transaction
from .promotion_schemas import (
    CurrentModelSelection,
    PromotionCreated,
    PromotionHistory,
    PromotionHistoryItem,
    PromotionModel,
    PromotionRecord,
    PromotionRequest,
)
from .tables import Experiment, Model, Promotion
from .timeutil import utc

MAX_HISTORY_LIMIT = 100


class PromotionNotFound(ValueError):
    pass


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("Expected a nonempty identity of at most 128 characters")
    return value.strip()


def _experiment(session, identity):
    experiment = session.get(Experiment, identity)
    if experiment is None:
        raise PromotionNotFound("Unknown experiment")
    return experiment


def _model_view(model):
    return PromotionModel(
        id=model.id,
        experiment_id=model.experiment_id,
        algorithm=model.algorithm,
        status=model.status,
        created_at=model.created_at,
        training_cutoff=model.training_cutoff,
        available_at=model.available_at,
        artifact=model.artifact,
    )


def _promotion_view(promotion):
    return PromotionRecord(
        id=promotion.id,
        experiment_id=promotion.experiment_id,
        model_id=promotion.model_id,
        created_at=promotion.created_at,
        reason=promotion.reason,
    )


def _encode_cursor(promotion):
    payload = {
        "experiment_id": promotion.experiment_id,
        "created_at": utc(promotion.created_at).isoformat(),
        "id": promotion.id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor, experiment_id):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 0 < len(cursor) <= 1024:
            raise ValueError("Invalid cursor length")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"experiment_id", "created_at", "id"}:
            raise ValueError("Invalid cursor fields")
        if payload["experiment_id"] != experiment_id or not isinstance(payload["created_at"], str):
            raise ValueError("Cursor belongs to another experiment or has an invalid time")
        return utc(payload["created_at"]), _identifier(payload["id"])
    except (ValueError, TypeError, binascii.Error) as error:
        raise ValueError("Invalid promotion history cursor for this experiment") from error


def get_promotion_history(experiment_id, *, limit=25, before=None) -> PromotionHistory:
    experiment_id = _identifier(experiment_id)
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError("Promotion history limit must be an integer between 1 and 100")
    cursor = _decode_cursor(before, experiment_id)
    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        _experiment(session, experiment_id)
        query = (
            select(Promotion, Model)
            .join(Model, Model.id == Promotion.model_id)
            .where(Promotion.experiment_id == experiment_id)
        )
        if cursor is not None:
            created_at, identity = cursor
            query = query.where(
                or_(
                    Promotion.created_at < created_at,
                    and_(Promotion.created_at == created_at, Promotion.id < identity),
                )
            )
        rows = session.execute(
            query.order_by(Promotion.created_at.desc(), Promotion.id.desc()).limit(limit + 1)
        ).all()
        visible = rows[:limit]
        more = len(rows) > limit
        return PromotionHistory(
            experiment_id=experiment_id,
            limit=limit,
            items=[
                PromotionHistoryItem(**_promotion_view(promotion).model_dump(), model=_model_view(model))
                for promotion, model in visible
            ],
            has_more=more,
            next_cursor=_encode_cursor(visible[-1][0]) if more else None,
        )


def get_current_selection(experiment_id, *, start=None) -> CurrentModelSelection:
    experiment_id = _identifier(experiment_id)
    if start is not None:
        if not isinstance(start, (datetime, str)):
            raise ValueError("Selection target must be an aware timestamp")
        start = utc(start)
    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        experiment = _experiment(session, experiment_id)
        if experiment.synthetic:
            raise ValueError("Current prospective model selection requires a real experiment")
        checked_at = forecast_execution.database_now(session)
        try:
            start = start if start is not None else checked_at + timedelta(minutes=30)
            end, cutoff = start + timedelta(days=1), start - timedelta(minutes=30)
        except OverflowError as error:
            raise ValueError("Selection target lies outside the supported timestamp range") from error
        if start <= checked_at:
            raise ValueError("Selection target must be in the future")
        base = {
            "experiment_id": experiment_id,
            "checked_at": checked_at,
            "start": start,
            "end": end,
            "input_cutoff": cutoff,
            "policy": forecast_execution.SELECTION_POLICY,
        }
        try:
            model, selection = forecast_execution.select_forecast_model(
                session, experiment_id, None, cutoff, checked_at
            )
        except ValueError as error:
            return CurrentModelSelection(
                **base,
                kind="unavailable",
                reason_code="selection_unavailable",
                reason=str(error),
                model=None,
                promotion=None,
            )
        promotion = session.get(Promotion, selection["promotion_id"]) if selection["promotion_id"] else None
        return CurrentModelSelection(
            **base,
            kind=selection["kind"],
            reason_code="latest_promotion" if promotion is not None else "historical_rate_fallback",
            reason=(
                "The latest eligible promotion determines the model."
                if promotion is not None
                else "No promotion is eligible at this time; the latest available historical-rate model is selected."
            ),
            model=_model_view(model),
            promotion=_promotion_view(promotion) if promotion is not None else None,
        )


def create_promotion(model_id, reason) -> PromotionCreated:
    request = PromotionRequest(model_id=model_id, reason=reason)
    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        model = session.get(Model, request.model_id)
        if model is None:
            raise PromotionNotFound("Unknown model")
        checked_at = forecast_execution.database_now(session)
        forecast_execution.select_forecast_model(
            session, model.experiment_id, model.id, checked_at, checked_at
        )
        promotion = Promotion(
            experiment_id=model.experiment_id,
            model_id=model.id,
            created_at=checked_at,
            reason=request.reason,
        )
        session.add(promotion)
        session.flush()
        return PromotionCreated(**_promotion_view(promotion).model_dump())
