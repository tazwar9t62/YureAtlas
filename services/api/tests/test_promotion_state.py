"""Promotion audit and shared selection policy; DB cases require the owned harness."""

import base64
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import joblib
import numpy as np
import pytest
from sqlalchemy import event, text
from yureatlas import forecast_execution, promotion_state
from yureatlas.artifacts import canonical, put
from yureatlas.catalogue import ensure_experiment, load_experiment
from yureatlas.db import engine, transaction
from yureatlas.promotion_state import PromotionNotFound
from yureatlas.rate import HistoricalRate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Model, Promotion
from yureatlas.timeutil import JST


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.fixture
def forbid_database(monkeypatch):
    monkeypatch.setattr(
        promotion_state, "transaction", lambda: pytest.fail("Invalid request reached database")
    )


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "25"])
def test_history_rejects_unbounded_or_noninteger_limits_before_database(forbid_database, limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        promotion_state.get_promotion_history("experiment", limit=limit)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not%%%base64",
        "a" * 1025,
        encoded({"created_at": "2025-01-01T00:00:00Z", "id": "row"}),
        encoded({"experiment_id": "foreign", "created_at": "2025-01-01T00:00:00Z", "id": "row"}),
        encoded({"experiment_id": "experiment", "created_at": "2025-01-01T00:00:00", "id": "row"}),
        encoded({"experiment_id": "experiment", "created_at": "2025-01-01T00:00:00Z", "id": ""}),
    ],
)
def test_history_rejects_malformed_or_foreign_cursor_before_database(forbid_database, cursor):
    with pytest.raises(ValueError, match="history cursor"):
        promotion_state.get_promotion_history("experiment", before=cursor)


@pytest.mark.parametrize("reason", ["", "   ", " four ", "x" * 501, 42])
def test_promotion_rejects_invalid_reason_before_database(forbid_database, reason):
    with pytest.raises(ValueError):
        promotion_state.create_promotion("model", reason)


@pytest.mark.parametrize("start", ["2025-01-01T00:00:00", 123])
def test_selection_rejects_naive_or_non_timestamp_start_before_database(forbid_database, start):
    with pytest.raises(ValueError):
        promotion_state.get_current_selection("experiment", start=start)


@pytest.fixture
def catalogue(monkeypatch):
    identity = "promotion_state_" + uuid4().hex[:12]
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
    with transaction() as session:
        wall = session.scalar(text("SELECT clock_timestamp()"))
    clock = SimpleNamespace(value=wall)
    monkeypatch.setattr(forecast_execution, "database_now", lambda _session: clock.value)
    fitted = HistoricalRate(np.full(len(grid.ids), 0.02), 1.4, 0.05, 5)
    fitted.fixture_identity = identity
    buffer = io.BytesIO()
    joblib.dump(fitted, buffer, compress=3)
    artifact = put(buffer.getvalue(), "joblib")

    def model(**changes):
        values = {
            "id": str(uuid4()),
            "experiment_id": experiment.id,
            "algorithm": "historical_rate",
            "status": "available",
            "created_at": wall - timedelta(hours=1),
            "training_cutoff": wall - timedelta(days=2),
            "available_at": wall - timedelta(hours=1),
            "artifact": artifact,
            "metadata_": {},
        }
        row = Model(**{**values, **changes})
        with transaction() as session:
            session.add(row)
            session.flush()
        return row

    def promote(model_id, **changes):
        values = {
            "id": str(uuid4()),
            "experiment_id": experiment.id,
            "model_id": model_id,
            "created_at": wall - timedelta(minutes=1),
            "reason": "Documented fixture selection",
        }
        row = Promotion(**{**values, **changes})
        with transaction() as session:
            session.add(row)
            session.flush()
        return row

    return SimpleNamespace(experiment=experiment, wall=wall, clock=clock, model=model, promote=promote)


@pytest.mark.integration
def test_current_selection_matches_real_policy_with_fallback_and_tied_promotions(catalogue):
    case = catalogue
    missing = promotion_state.get_current_selection(case.experiment.id)
    assert missing.kind == "unavailable" and missing.model is None and missing.promotion is None
    assert "No eligible model" in missing.reason
    suffix = uuid4().hex
    high = case.model(id="f" + suffix)
    low = case.model(id="0" + suffix)  # Opposite insertion order to ID precedence.
    fallback = promotion_state.get_current_selection(case.experiment.id)
    assert fallback.kind == "historical_rate" and fallback.reason_code == "historical_rate_fallback"
    assert fallback.model.id == high.id and fallback.promotion is None
    assert fallback.start == case.wall + timedelta(minutes=30) and fallback.input_cutoff == case.wall
    assert fallback.end - fallback.start == timedelta(days=1)
    chosen = case.promote(low.id, id="f" + suffix, reason="Prefer the lower-ID model for this experiment")
    case.promote(high.id, id="0" + suffix)
    current = promotion_state.get_current_selection(case.experiment.id)
    with transaction() as session:
        actual_model, evidence = forecast_execution.select_forecast_model(
            session, case.experiment.id, None, case.wall, case.wall
        )
    assert current.kind == "promotion" and current.reason_code == "latest_promotion"
    assert current.model.id == actual_model.id == low.id
    assert current.promotion.id == evidence["promotion_id"] == chosen.id
    assert current.promotion.reason == chosen.reason and current.policy == evidence["policy"]


@pytest.mark.integration
def test_history_cursor_preserves_tied_rows_and_excludes_later_insert_without_n_plus_one(catalogue):
    case = catalogue
    model = case.model()
    suffix = uuid4().hex
    rows = {
        letter: case.promote(
            model.id,
            id=letter + suffix,
            created_at=case.wall - timedelta(days=1 if letter == "e" else 0, minutes=1),
            reason=f"Audit rationale for entry {letter}",
        )
        for letter in ("a", "d", "b", "c", "e")
    }
    selects = []

    def count_selects(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "after_cursor_execute", count_selects)
    try:
        first = promotion_state.get_promotion_history(case.experiment.id, limit=2)
    finally:
        event.remove(engine, "after_cursor_execute", count_selects)
    assert len(selects) == 2  # Experiment existence plus one joined, bounded history query.
    assert [item.id for item in first.items] == [rows[letter].id for letter in ("d", "c")]
    assert first.has_more and first.next_cursor
    newest = case.promote(model.id, created_at=case.wall, reason="Inserted between history page requests")
    second = promotion_state.get_promotion_history(case.experiment.id, limit=2, before=first.next_cursor)
    third = promotion_state.get_promotion_history(case.experiment.id, limit=2, before=second.next_cursor)
    items = first.items + second.items + third.items
    assert [item.id for item in items] == [rows[letter].id for letter in ("d", "c", "b", "a", "e")]
    assert newest.id not in {item.id for item in items}
    assert not third.has_more and third.next_cursor is None
    assert all(item.model.id == model.id and item.model.experiment_id == case.experiment.id for item in items)
    assert items[0].reason == rows["d"].reason


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["unavailable", "foreign"])
def test_invalid_latest_promotion_is_reported_without_silent_fallback(catalogue, invalid):
    case = catalogue
    baseline = case.model()
    if invalid == "unavailable":
        selected = case.model(status="insufficient_data", artifact=None)
    else:
        foreign = ensure_experiment(
            ExperimentSpec(
                name="foreign_promotion_" + uuid4().hex[:12],
                provider="synthetic",
                west=140,
                east=141,
                south=35,
                north=36,
            )
        )
        selected = case.model(experiment_id=foreign.id)
    audit = case.promote(selected.id)
    current = promotion_state.get_current_selection(case.experiment.id)
    assert current.kind == "unavailable" and current.reason_code == "selection_unavailable"
    assert current.model is None and current.promotion is None
    assert ("unavailable" if invalid == "unavailable" else "incompatible") in current.reason
    history = promotion_state.get_promotion_history(case.experiment.id)
    assert history.items[0].id == audit.id and history.items[0].model_id != baseline.id


@pytest.mark.integration
def test_explicit_target_uses_its_cutoff_and_equivalent_timezone_inputs(catalogue):
    case = catalogue
    candidate = case.model(training_cutoff=case.wall - timedelta(minutes=10))
    assert promotion_state.get_current_selection(case.experiment.id).model.id == candidate.id
    start = case.wall + timedelta(minutes=15)
    first = promotion_state.get_current_selection(case.experiment.id, start=start)
    second = promotion_state.get_current_selection(
        case.experiment.id, start=start.astimezone(JST).isoformat()
    )
    assert first == second and first.input_cutoff == case.wall - timedelta(minutes=15)
    assert first.kind == "unavailable" and "training cutoff" in first.reason
    with pytest.raises(ValueError, match="in the future"):
        promotion_state.get_current_selection(case.experiment.id, start=case.wall)


@pytest.mark.integration
def test_creation_appends_trimmed_reasons_preserves_old_audit_and_changes_current_selection(catalogue):
    case = catalogue
    first_model, second_model = case.model(), case.model()
    first = promotion_state.create_promotion(first_model.id, "  First model selected after review  ")
    before = canonical(first.model_dump())
    case.clock.value += timedelta(seconds=1)
    second = promotion_state.create_promotion(second_model.id, "Second model selected after further review")
    assert first.reason == "First model selected after review"
    assert first.affects == second.affects == "future forecasts only"
    current = promotion_state.get_current_selection(case.experiment.id)
    assert current.model.id == second_model.id and current.promotion.id == second.id
    history = promotion_state.get_promotion_history(case.experiment.id)
    assert [item.id for item in history.items] == [second.id, first.id]
    archived_first = history.items[1].model_dump(exclude={"model"})
    assert canonical({**archived_first, "affects": first.affects}) == before
    invalid = case.model(available_at=case.clock.value + timedelta(days=1))
    with pytest.raises(ValueError, match="not yet available"):
        promotion_state.create_promotion(invalid.id, "Cannot select an unavailable future artifact")
    assert [item.id for item in promotion_state.get_promotion_history(case.experiment.id).items] == [
        second.id,
        first.id,
    ]


@pytest.mark.integration
def test_empty_and_unknown_histories_and_synthetic_selection_are_explicit(catalogue):
    case = catalogue
    history = promotion_state.get_promotion_history(case.experiment.id)
    assert history.items == [] and not history.has_more and history.next_cursor is None
    with pytest.raises(PromotionNotFound, match="Unknown experiment"):
        promotion_state.get_promotion_history(uuid4().hex)
    with pytest.raises(PromotionNotFound, match="Unknown experiment"):
        promotion_state.get_current_selection(uuid4().hex)
    with pytest.raises(PromotionNotFound, match="Unknown model"):
        promotion_state.create_promotion(uuid4().hex, "A valid reason for a nonexistent model")
    synthetic = ensure_experiment(
        ExperimentSpec(
            name="synthetic_promotion_" + uuid4().hex[:12],
            provider="synthetic",
            west=140,
            east=141,
            south=35,
            north=36,
        )
    )
    model = case.model(experiment_id=synthetic.id)
    with pytest.raises(ValueError, match="real experiment"):
        promotion_state.get_current_selection(synthetic.id)
    with pytest.raises(ValueError, match="Synthetic and real"):
        promotion_state.create_promotion(model.id, "Synthetic models must remain separate")
    assert promotion_state.get_promotion_history(synthetic.id).items == []
