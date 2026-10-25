"""A metric report and its download describe one bounded, reproducible scope."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from test_comparison_pairing import (
    archived_comparison as archived_comparison,  # noqa: PLC0414 - pytest fixture
)
from yureatlas import api, reports
from yureatlas.db import engine
from yureatlas.timeutil import utc

START = utc("2024-12-31T15:00:00Z")  # New Year's midnight JST.
END = START + timedelta(days=2)
NOW = utc("2025-01-04T12:00:00Z")
MODE = "synthetic_demo"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(reports, "now", lambda: NOW)
    with TestClient(api.app) as requests:
        yield requests


def params(experiment, **scope):
    return {"experiment_id": experiment.id, "mode": MODE, **scope}


def body(response):
    assert response.status_code == 200, response.text
    return response.json()


def forecast_ids(report):
    return {row["forecast_id"] for row in report["decisions"]}


@pytest.mark.parametrize("path", ["/metrics", "/exports/metrics"])
@pytest.mark.parametrize(
    "scope",
    [
        {"mode": "unsupported"},
        {"days": -1},
        {"days": 3651},
        {"days": "not-an-integer"},
        {"start": START.isoformat()},
        {"end": END.isoformat()},
        {"start": START.isoformat(), "end": END.isoformat(), "days": 1},
        {"start": START.isoformat(), "end": END.isoformat(), "days": 0},
        {"start": "2025-01-01T00:00:00", "end": "2025-01-03T00:00:00"},
        {"start": END.isoformat(), "end": START.isoformat()},
        {"start": START.isoformat(), "end": START.isoformat()},
        {"start": START.isoformat(), "end": (START + timedelta(days=3650, seconds=1)).isoformat()},
        {"start": "not-a-date", "end": END.isoformat()},
    ],
)
def test_invalid_report_scope_is_rejected_before_database(client, monkeypatch, path, scope):
    def forbidden_database():
        raise AssertionError("Invalid report parameters must fail before reading the database")

    monkeypatch.setattr(reports, "transaction", forbidden_database)
    response = client.get(path, params={"experiment_id": "unused", "mode": MODE, **scope})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("path", ["/metrics", "/exports/metrics"])
@pytest.mark.parametrize("sqlstate", ["57014", "08006"])
def test_only_statement_cancellation_is_reported_as_a_retryable_scope_timeout(
    client, monkeypatch, path, sqlstate
):
    original = RuntimeError("Simulated PostgreSQL error")
    original.sqlstate = sqlstate
    failure = DBAPIError("bounded report query", {}, original)

    def fail_collection(*args, **kwargs):
        raise failure

    monkeypatch.setattr(reports, "_collect", fail_collection)
    if sqlstate == "57014":
        response = client.get(path, params={"experiment_id": "unused", "mode": MODE, "days": 30})
        assert response.status_code == 503, response.text
        assert "narrower" in response.json()["detail"] and "retry" in response.json()["detail"]
    else:
        with pytest.raises(DBAPIError) as caught:
            client.get(path, params={"experiment_id": "unused", "mode": MODE, "days": 30})
        assert caught.value is failure


@pytest.mark.integration
def test_absolute_scope_includes_only_whole_windows_and_timezone_equivalent_requests_match(
    client, archived_comparison
):
    experiment, publish, _ = archived_comparison
    inside = [publish(start=START), publish(start=START + timedelta(days=1))]
    no_evaluation = publish(start=START, evaluate=False)
    awaiting = publish(start=START + timedelta(days=1), status="awaiting_data")
    outside = [
        publish(start=START - timedelta(days=1)),
        publish(start=END),
        publish(start=START - timedelta(hours=1)),
        publish(start=END - timedelta(hours=23)),
        publish(start=START - timedelta(hours=1), end=END + timedelta(hours=1)),
        publish(start=START, mode="retrospective_revised"),
    ]
    utc_report = body(
        client.get("/metrics", params=params(experiment, start=START.isoformat(), end=END.isoformat()))
    )
    jst_report = body(
        client.get(
            "/metrics",
            params=params(experiment, start="2025-01-01T00:00:00+09:00", end="2025-01-03T00:00:00+09:00"),
        )
    )
    assert jst_report == utc_report
    assert utc_report["scope"]["kind"] == "explicit"
    assert utc_report["scope"]["requested_days"] is None
    assert utc(utc_report["scope"]["requested_start"]) == START
    assert utc(utc_report["scope"]["requested_end"]) == END
    assert utc(utc_report["scope"]["resolved_start"]) == START
    assert utc(utc_report["scope"]["resolved_end"]) == END
    assert utc_report["scope"]["display_timezone"] == "Asia/Tokyo"
    assert forecast_ids(utc_report) == {row.id for row in [*inside, no_evaluation, awaiting]}
    assert utc_report["selected_forecast_ids"] == sorted(forecast_ids(utc_report))
    assert utc_report["selected_evaluation_ids"] == sorted(
        row["evaluation_id"] for row in utc_report["decisions"] if row["evaluation_id"] is not None
    )
    assert utc_report["counts"]["forecast_count"] == 4 and utc_report["counts"]["evaluation_count"] == 3
    assert not forecast_ids(utc_report) & {row.id for row in outside}
    decisions = {row["forecast_id"]: row for row in utc_report["decisions"]}
    assert decisions[no_evaluation.id]["reasons"] == ["candidate_not_evaluated"]
    assert decisions[awaiting.id]["reasons"] == ["candidate_observations_unavailable"]
    exported = client.get(
        "/exports/metrics", params=params(experiment, start=START.isoformat(), end=END.isoformat())
    )
    assert body(exported) == utc_report
    assert "attachment" in exported.headers["content-disposition"]


@pytest.mark.integration
def test_relative_period_anchors_to_latest_completed_evaluated_window_including_awaiting(
    client, archived_comparison
):
    experiment, publish, _ = archived_comparison
    old = publish(start=START)
    awaiting = publish(start=START + timedelta(days=1), status="awaiting_data")
    same_window_no_evaluation = publish(start=START + timedelta(days=1), evaluate=False)
    later_no_evaluation = publish(start=END, evaluate=False)
    future_evaluation = publish(start=NOW + timedelta(days=1))
    report = body(client.get("/metrics", params=params(experiment, days=1)))
    assert report["scope"]["kind"] == "relative" and report["scope"]["requested_days"] == 1
    assert utc(report["scope"]["anchor"]) == END
    assert utc(report["scope"]["resolved_start"]) == END - timedelta(days=1)
    assert utc(report["scope"]["resolved_end"]) == END
    assert forecast_ids(report) == {awaiting.id, same_window_no_evaluation.id}
    assert not forecast_ids(report) & {old.id, later_no_evaluation.id, future_evaluation.id}
    assert body(client.get("/exports/metrics", params=params(experiment, days=1))) == report


@pytest.mark.integration
def test_empty_relative_explicit_and_whole_experiment_reports_preserve_their_distinct_scope_policies(
    client, archived_comparison
):
    experiment, publish, _ = archived_comparison
    relative = body(client.get("/metrics", params=params(experiment)))
    assert relative["scope"]["kind"] == "relative" and relative["scope"]["requested_days"] == 30
    assert relative["scope"]["empty_reason"] == "no_completed_evaluated_window"
    assert relative["scope"]["resolved_start"] is None and relative["scope"]["resolved_end"] is None
    assert relative["scope"]["anchor"] is None and forecast_ids(relative) == set()
    assert body(client.get("/metrics", params=params(experiment, days=30))) == relative

    explicit = body(
        client.get("/metrics", params=params(experiment, start=START.isoformat(), end=END.isoformat()))
    )
    assert explicit["scope"]["empty_reason"] == "no_forecasts_in_range"
    assert utc(explicit["scope"]["resolved_start"]) == START
    assert utc(explicit["scope"]["resolved_end"]) == END
    assert forecast_ids(explicit) == set()

    whole_empty = body(client.get("/metrics", params=params(experiment, days=0)))
    assert whole_empty["scope"]["kind"] == "whole_experiment"
    assert whole_empty["scope"]["empty_reason"] == "no_forecasts"
    assert whole_empty["scope"]["resolved_start"] is None and whole_empty["scope"]["resolved_end"] is None

    unevaluated = publish(start=START, evaluate=False)
    future = publish(start=NOW + timedelta(days=1))
    still_unanchored = body(client.get("/metrics", params=params(experiment, days=30)))
    assert still_unanchored["scope"]["empty_reason"] == "no_completed_evaluated_window"
    assert forecast_ids(still_unanchored) == set()
    whole = body(client.get("/metrics", params=params(experiment, days=0)))
    assert forecast_ids(whole) == {unevaluated.id, future.id}
    assert utc(whole["scope"]["resolved_start"]) == START
    assert utc(whole["scope"]["resolved_end"]) == future.end
    assert whole["scope"]["empty_reason"] is None
    assert body(client.get("/exports/metrics", params=params(experiment, days=0))) == whole


@pytest.mark.integration
def test_whole_history_rejects_a_span_beyond_the_same_explicit_range_bound(client, archived_comparison):
    experiment, publish, _ = archived_comparison
    publish(start=START - timedelta(days=3650))
    publish(start=START)
    response = client.get("/metrics", params=params(experiment, days=0))
    assert response.status_code == 422, response.text
    maximum_valid = body(
        client.get(
            "/metrics",
            params=params(
                experiment, start=(START - timedelta(days=3650)).isoformat(), end=START.isoformat()
            ),
        )
    )
    assert len(forecast_ids(maximum_valid)) == 1
    # The old archive remains inspectable through a bounded explicit window.
    narrowed = body(
        client.get("/metrics", params=params(experiment, start=START.isoformat(), end=END.isoformat()))
    )
    assert len(forecast_ids(narrowed)) == 1


@pytest.mark.integration
def test_export_precondition_detects_new_evaluation_and_does_not_change_loaded_report(
    client, archived_comparison
):
    experiment, publish, append_evaluation = archived_comparison
    publish(start=START, algorithm="historical_rate")
    candidate = publish(start=START)
    scope = params(experiment, start=START.isoformat(), end=END.isoformat())
    loaded = body(client.get("/metrics", params=scope))
    original = deepcopy(loaded)
    report_id = loaded["report_id"]
    assert body(client.get("/exports/metrics", params={**scope, "expected_report_id": report_id})) == loaded
    evaluation_id = append_evaluation(candidate, status="awaiting_data", version=2)
    stale = client.get("/exports/metrics", params={**scope, "expected_report_id": report_id})
    assert stale.status_code == 409, stale.text
    current = body(client.get("/metrics", params=scope))
    assert current["report_id"] != report_id
    assert (
        next(row for row in current["decisions"] if row["forecast_id"] == candidate.id)["evaluation_id"]
        == evaluation_id
    )
    assert loaded == original
    assert json.loads(json.dumps(loaded, allow_nan=False)) == original


@pytest.mark.integration
def test_report_identity_ignores_generation_time_but_includes_resolved_request_scope(
    client, archived_comparison, monkeypatch
):
    experiment, publish, _ = archived_comparison
    publish(start=START)
    scope = params(experiment, start=START.isoformat(), end=END.isoformat())
    first = body(client.get("/metrics", params=scope))
    monkeypatch.setattr(reports, "now", lambda: NOW + timedelta(seconds=1))
    later = body(client.get("/metrics", params=scope))
    assert later["generated_at"] != first["generated_at"]
    assert later["report_id"] == first["report_id"]
    assert {key: value for key, value in later.items() if key != "generated_at"} == {
        key: value for key, value in first.items() if key != "generated_at"
    }
    broader = body(
        client.get(
            "/metrics",
            params=params(experiment, start=(START - timedelta(hours=1)).isoformat(), end=END.isoformat()),
        )
    )
    assert forecast_ids(broader) == forecast_ids(first)
    assert broader["report_id"] != first["report_id"]


@pytest.mark.integration
def test_query_count_does_not_grow_per_forecast_or_evaluation(client, archived_comparison):
    experiment, publish, _ = archived_comparison
    publish(start=START, algorithm="historical_rate")
    publish(start=START)
    scope = params(experiment, start=START.isoformat(), end=END.isoformat())

    def measured_report():
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            report = body(client.get("/metrics", params=scope))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        return report, statements

    small, small_queries = measured_report()
    for _ in range(10):
        publish(start=START)
    large, large_queries = measured_report()
    assert len(forecast_ids(small)) == 2 and len(forecast_ids(large)) == 12
    assert len(large_queries) <= len(small_queries) + 1
    assert len(large_queries) <= 12


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["MAX_FORECASTS", "MAX_CELL_ROWS", "MAX_GRID_CELLS"])
def test_resource_bounds_accept_exact_limit_and_reject_overflow_without_truncating(
    client, archived_comparison, monkeypatch, limit
):
    experiment, publish, _ = archived_comparison
    publish(start=START, algorithm="historical_rate")
    publish(start=START)
    scope = params(experiment, start=START.isoformat(), end=END.isoformat())
    original = body(client.get("/metrics", params=scope))
    counts = original["counts"]
    exact = {
        "MAX_FORECASTS": counts["forecast_count"],
        "MAX_CELL_ROWS": counts["forecast_cell_rows"] + counts["evaluation_cell_rows"],
        "MAX_GRID_CELLS": counts["forecast_cell_rows"] // counts["forecast_count"],
    }[limit]
    monkeypatch.setattr(reports, limit, exact)
    bounded = body(client.get("/metrics", params=scope))
    assert bounded["counts"] == counts and forecast_ids(bounded) == forecast_ids(original)
    assert bounded["comparisons"] == original["comparisons"]
    monkeypatch.setattr(reports, limit, exact - 1)
    for path in ("/metrics", "/exports/metrics"):
        response = client.get(path, params=scope)
        assert response.status_code == 422, response.text
        assert "narrower" in response.json()["detail"]
        assert "series" not in response.json()


@pytest.mark.integration
def test_report_keeps_one_database_snapshot_when_an_evaluation_is_appended_mid_read(
    client, archived_comparison
):
    experiment, publish, append_evaluation = archived_comparison
    publish(start=START, algorithm="historical_rate")
    candidate = publish(start=START)
    scope = params(experiment, start=START.isoformat(), end=END.isoformat())
    original = body(client.get("/metrics", params=scope))
    appended = []

    def after_identity_read(connection, cursor, statement, parameters, context, executemany):
        if not appended and "FROM experiment_versions" in statement:
            # The report has established its read snapshot. Commit a new immutable
            # evaluation through a separate connection before its later data reads.
            appended.append("writing")
            appended[0] = append_evaluation(candidate, status="awaiting_data", version=2)

    event.listen(engine, "after_cursor_execute", after_identity_read)
    try:
        during_write = body(client.get("/metrics", params=scope))
    finally:
        event.remove(engine, "after_cursor_execute", after_identity_read)
    assert len(appended) == 1 and appended[0] != "writing"
    assert during_write == original
    after_write = body(client.get("/metrics", params=scope))
    assert after_write["report_id"] != original["report_id"]
    assert appended[0] in after_write["selected_evaluation_ids"]


@pytest.mark.integration
def test_unknown_experiment_is_distinct_from_a_known_empty_report(client):
    for path in ("/metrics", "/exports/metrics"):
        response = client.get(path, params={"experiment_id": str(uuid4()), "mode": MODE, "days": 30})
        assert response.status_code == 404, response.text
