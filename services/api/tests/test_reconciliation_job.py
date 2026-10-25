"""A scheduled refresh and its evaluation have an explicit execution order."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from yureatlas import tasks
from yureatlas.timeutil import utc

END = utc("2025-01-01T06:30:00+09:00")
PAYLOAD = {
    "experiment_id": "experiment",
    "start": (END - timedelta(days=31)).isoformat(),
    "end": END.isoformat(),
}


@pytest.fixture
def execution(monkeypatch):
    calls, progress = [], []
    experiment = SimpleNamespace(synthetic=False, config={"provider": "usgs"})
    monkeypatch.setattr(tasks, "load_experiment", lambda _identity: (experiment, None))
    monkeypatch.setattr(tasks, "now", lambda: END)

    @contextmanager
    def read_forecasts():
        calls.append("read_forecasts")
        yield SimpleNamespace(scalars=lambda _query: ["forecast"])

    def evaluate(identity):
        calls.append("evaluate")
        assert identity == "forecast"
        return SimpleNamespace(id="evaluation", status="unavailable")

    monkeypatch.setattr(tasks, "transaction", read_forecasts)
    monkeypatch.setattr(tasks, "evaluate_forecast", evaluate)
    return SimpleNamespace(calls=calls, progress=progress, experiment=experiment)


@pytest.mark.parametrize("refresh_status", ["complete", "partial"])
def test_usgs_refresh_finishes_before_observation_selection(execution, monkeypatch, refresh_status):
    def ingest(identity, start, end, progress, *, use_feed):
        execution.calls.append("ingest")
        assert identity == "experiment" and start == END - timedelta(days=31) and end == END
        assert use_feed is False
        progress(0.5)
        progress(1)
        execution.calls.append("refreshed")
        return {"status": refresh_status}

    monkeypatch.setattr(tasks, "ingest_usgs", ingest)
    result = tasks.execute("reconcile", PAYLOAD, execution.progress.append)
    assert execution.calls == ["ingest", "refreshed", "read_forecasts", "evaluate"]
    assert result == {
        "experiment_id": "experiment",
        "scheduled_end": END.isoformat(),
        "ingestion": {"status": refresh_status},
        "evaluations": [{"id": "evaluation", "status": "unavailable"}],
    }
    assert execution.progress == sorted(execution.progress)
    assert execution.progress[0] == 0 and execution.progress[-1] == 1


def test_failed_refresh_never_starts_evaluation(execution, monkeypatch):
    def fail(*_args, **_kwargs):
        execution.calls.append("ingest")
        raise RuntimeError("Provider interrupted")

    monkeypatch.setattr(tasks, "ingest_usgs", fail)
    with pytest.raises(RuntimeError, match="Provider interrupted"):
        tasks.execute("reconcile", PAYLOAD, execution.progress.append)
    assert execution.calls == ["ingest"]


def test_jma_uses_imported_evidence_without_network_refresh(execution, monkeypatch):
    execution.experiment.config["provider"] = "jma"
    monkeypatch.setattr(tasks, "ingest_usgs", lambda *_args, **_kwargs: pytest.fail("JMA network refresh"))
    result = tasks.execute("reconcile", PAYLOAD, execution.progress.append)
    assert execution.calls == ["read_forecasts", "evaluate"]
    assert result["ingestion"] is None and result["evaluations"][0]["id"] == "evaluation"


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"experiment_id": None}, "pinned interval"),
        ({"end": None}, "pinned interval"),
        ({"start": END.isoformat()}, "completed 31-day"),
        ({"start": (END - timedelta(days=32)).isoformat()}, "completed 31-day"),
        (
            {"start": (END - timedelta(days=30)).isoformat(), "end": (END + timedelta(days=1)).isoformat()},
            "completed 31-day",
        ),
        ({"end": END.replace(tzinfo=None).isoformat()}, "timezone"),
    ],
)
def test_invalid_reconciliation_rejects_before_source_work(execution, monkeypatch, changes, message):
    monkeypatch.setattr(tasks, "load_experiment", lambda *_: pytest.fail("Invalid request read source"))
    with pytest.raises(ValueError, match=message):
        tasks.execute("reconcile", {**PAYLOAD, **changes}, execution.progress.append)
    assert not execution.calls and not execution.progress


def test_reconciliation_cannot_operate_on_synthetic_experiment(execution):
    execution.experiment.synthetic = True
    with pytest.raises(ValueError, match="real USGS or JMA"):
        tasks.execute("reconcile", PAYLOAD, execution.progress.append)
    assert not execution.calls and not execution.progress
