from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from yureatlas import artifacts, schedule_configuration
from yureatlas import readiness_prerequisites as prerequisites
from yureatlas.catalogue import ensure_experiment
from yureatlas.db import transaction
from yureatlas.schedule_schemas import ScheduleUpdate
from yureatlas.schemas import ExperimentSpec
from yureatlas.tables import Experiment, Ingestion, Model, Promotion

CHECKED = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)


class ReadSession:
    """Only supplied SELECT results are available; no transaction or writes."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.statements = []

    def execute(self, statement):
        assert statement.is_select
        self.statements.append(statement.compile(dialect=postgresql.dialect()))
        response = self.responses.pop(0)
        return SimpleNamespace(mappings=lambda: iter(response), one=lambda: response)


def experiment(identity="e1", source_id="s1", provider="usgs"):
    return {"id": identity, "name": identity, "source_id": source_id, "provider": provider}


def receipt(source_id="s1", status="complete", **extra):
    return {
        "source_id": source_id,
        "id": "receipt-" + source_id,
        "received_at": CHECKED - timedelta(minutes=1),
        "start": CHECKED - timedelta(days=2),
        "end": CHECKED,
        "status": status,
        "coverage_effect": None,
        "feed": None,
        **extra,
    }


@pytest.fixture
def requested(monkeypatch):
    value = {
        "revision": 7,
        "spec": {"enabled": True, "scope": "all_real", "experiment_ids": [], "kinds": ["forecast"]},
    }
    monkeypatch.setattr(prerequisites, "load_schedule", lambda session: value)
    return value


@pytest.fixture
def selections(monkeypatch):
    calls = []

    def select_model(session, experiment_id, requested_model_id, cutoff, selected_at):
        calls.append((session, experiment_id, requested_model_id, cutoff, selected_at))
        return (
            SimpleNamespace(
                id="model-" + experiment_id,
                algorithm="historical_rate",
                training_cutoff=CHECKED - timedelta(days=1),
                available_at=CHECKED - timedelta(hours=1),
            ),
            {"kind": "historical_rate", "promotion_id": None},
        )

    monkeypatch.setattr(prerequisites, "select_forecast_model", select_model)
    return calls


@pytest.mark.parametrize(
    ("existence", "expected"),
    [((False, False), "first_run"), ((True, False), "real_setup_needed"), ((True, True), "empty_scope")],
)
def test_empty_scope_distinguishes_installation_setup(requested, selections, existence, expected):
    requested["spec"].update(scope="selected", experiment_ids=[], enabled=False, kinds=[])
    session = ReadSession([], existence)
    result = prerequisites.inspect_prerequisites(session, CHECKED)
    assert result.status == expected
    assert result.advisory and result.inspected_count == 0
    assert result.scope_complete and result.has_more is False
    assert selections == []
    assert len(session.statements) == 2
    assert "EXISTS" in str(session.statements[1])
    assert "count(" not in str(session.statements[1]).lower()


def test_disabled_settings_still_show_real_prerequisites(requested, selections):
    requested["spec"].update(enabled=False, kinds=[], scope="selected", experiment_ids=["e1"])
    session = ReadSession([experiment()], [receipt()])
    result = prerequisites.inspect_prerequisites(session, CHECKED)
    assert result.status == "metadata_available"
    assert result.schedule_revision == 7 and result.scope == "selected"
    assert result.selected_experiment_ids == ["e1"]
    assert result.selection_start == CHECKED + timedelta(minutes=30)
    assert result.selection_input_cutoff == CHECKED
    assert selections == [(session, "e1", None, CHECKED, CHECKED)]
    assert result.experiments[0].source.coverage_verified is False
    assert "not establish completeness" in result.caveat
    assert "artifact bytes were not inspected" in result.experiments[0].model.message.lower()
    assert "e1" in session.statements[0].params.values() or ["e1"] in session.statements[0].params.values()


@pytest.mark.parametrize(
    "latest",
    [
        None,
        receipt(status="partial"),
        receipt(status="failed"),
        receipt(coverage_effect="supplement"),
        receipt(feed="true"),
    ],
)
def test_incomplete_or_supplemental_source_is_advisory(requested, selections, latest):
    no_receipt = dict(receipt(), id=None)
    result = prerequisites.inspect_prerequisites(ReadSession([experiment()], [latest or no_receipt]), CHECKED)
    assert result.status == "needs_attention"
    assert result.advisory
    item = result.experiments[0]
    assert item.attention_needed and item.source.coverage_verified is False
    assert item.model.status == "metadata_available"
    assert item.source.status == ("no_receipt" if latest is None else "receipt_observed")


def test_unavailable_model_retains_receipt_and_explains_selection_failure(requested, monkeypatch):
    def missing_model(*args):
        raise ValueError("Selected forecast model is unavailable")

    monkeypatch.setattr(prerequisites, "select_forecast_model", missing_model)
    result = prerequisites.inspect_prerequisites(ReadSession([experiment()], [receipt()]), CHECKED)
    assert result.status == "needs_attention" and result.advisory
    item = result.experiments[0]
    assert item.source.latest_receipt.status == "complete"
    assert item.model.status == "unavailable" and item.model.model_id is None
    assert item.model.message == "Selected forecast model is unavailable"


def test_sample_never_selects_more_than_twenty_models(requested, selections):
    rows = [experiment(f"e{index}", f"s{index}") for index in range(21)]
    session = ReadSession(rows, [receipt(f"s{index}") for index in range(20)])
    result = prerequisites.inspect_prerequisites(session, CHECKED)
    assert result.status == "sampled"
    assert result.has_more and not result.scope_complete
    assert result.inspected_count == result.inspection_limit == len(result.experiments) == 20
    assert len(selections) == 20
    assert {call[1] for call in selections} == {f"e{index}" for index in range(20)}
    assert 21 in session.statements[0].params.values()
    assert list(session.statements[1].params.values()).count(sorted(f"s{i}" for i in range(20))) == 1
    assert len(session.statements) == 2


def test_shared_source_receipt_is_loaded_once_and_has_bounded_projection(requested, selections):
    session = ReadSession([experiment("e1"), experiment("e2")], [receipt()])
    result = prerequisites.inspect_prerequisites(session, CHECKED)
    assert result.inspected_count == 2 and len(selections) == 2
    assert result.experiments[0].source.latest_receipt == result.experiments[1].source.latest_receipt
    sql = str(session.statements[1])
    assert "LATERAL" in sql and "LIMIT" in sql
    assert "ingestion_runs.created_at DESC, ingestion_runs.id DESC" in sql
    assert "ingestion_runs.created_at <=" in sql
    assert "earthquake_events" not in sql and "earthquake_revisions" not in sql
    assert "ingestion_runs.report," not in sql
    assert 1 in session.statements[1].params.values()


def test_unexpected_selection_failure_reaches_infrastructure_probe(requested, monkeypatch):
    failure = RuntimeError("database probe interrupted")

    def interrupted(*args):
        raise failure

    monkeypatch.setattr(prerequisites, "select_forecast_model", interrupted)
    with pytest.raises(RuntimeError) as raised:
        prerequisites.inspect_prerequisites(ReadSession([experiment()], [receipt()]), CHECKED)
    assert raised.value is failure


def test_unavailable_without_database_has_unknown_scope():
    result = prerequisites.ReadinessPrerequisites(status="unavailable", message="Database schema unavailable")
    assert result.advisory
    assert result.checked_at is result.scope is result.scope_complete is result.has_more is None
    assert result.inspected_count == 0 and result.experiments == []
    assert result.model_dump(mode="json")["inspection_limit"] == 20


def test_naive_probe_clock_fails_before_reading_settings(monkeypatch):
    def unexpected(*args):
        raise AssertionError("Settings must not be read for a naive clock")

    monkeypatch.setattr(prerequisites, "load_schedule", unexpected)
    with pytest.raises(ValueError, match="timezone"):
        prerequisites.inspect_prerequisites(ReadSession(), CHECKED.replace(tzinfo=None))


@pytest.fixture
def owned_prerequisites(monkeypatch):
    with transaction() as session:
        original = schedule_configuration.load_schedule(session)

    def create(provider="jma"):
        identity = "prerequisite_" + uuid4().hex[:16]
        return ensure_experiment(
            ExperimentSpec(
                name=identity,
                provider=provider,
                catalogue_namespace=identity if provider == "jma" else None,
                west=140,
                east=140.5,
                south=35,
                north=35.5,
            )
        )

    def configure(ids, *, scope="selected"):
        with transaction() as session:
            current = schedule_configuration.load_schedule(session)
        return schedule_configuration.update_schedule(
            ScheduleUpdate(
                expected_revision=current["revision"],
                spec={"enabled": False, "scope": scope, "experiment_ids": ids, "kinds": []},
                reason="Owned prerequisite acceptance scope",
            )
        )

    def forbid_artifact_read(*args, **kwargs):
        pytest.fail("Metadata prerequisites must not inspect model artifact bytes")

    monkeypatch.setattr(artifacts, "read", forbid_artifact_read)
    real = [create(), create()]
    synthetic = create("synthetic")
    configure([item.id for item in real])
    try:
        yield SimpleNamespace(real=real, synthetic=synthetic, create=create, configure=configure)
    finally:
        with transaction() as session:
            current = schedule_configuration.load_schedule(session)
        # Keep append-only history intact, including on failed assertions.
        schedule_configuration.update_schedule(
            ScheduleUpdate(
                expected_revision=current["revision"],
                spec=original["spec"],
                reason="Restore policy after owned prerequisite acceptance",
            )
        )


@pytest.mark.integration
def test_postgis_latest_receipt_and_actual_promotion_selection_are_advisory(owned_prerequisites):
    case = owned_prerequisites
    selected, empty = case.real
    with transaction() as session:
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        suffix = uuid4().hex
        receipt_time = checked_at - timedelta(minutes=1)
        newest = Ingestion(
            id="f" + suffix,
            source_id=selected.source_id,
            created_at=receipt_time,
            start=checked_at - timedelta(days=2),
            end=checked_at,
            status="partial",
            report={"coverage_effect": "supplement", "feed": True},
        )
        older_tie = Ingestion(
            id="0" + suffix,
            source_id=selected.source_id,
            created_at=receipt_time,
            start=checked_at - timedelta(days=2),
            end=checked_at,
            status="complete",
            report={},
        )
        future = Ingestion(
            source_id=selected.source_id,
            created_at=checked_at + timedelta(days=1),
            start=checked_at - timedelta(days=1),
            end=checked_at,
            status="failed",
            report={},
        )
        model = Model(
            id=str(uuid4()),
            experiment_id=selected.id,
            algorithm="hybrid",
            status="available",
            created_at=checked_at - timedelta(days=1),
            training_cutoff=checked_at - timedelta(days=2),
            available_at=checked_at - timedelta(hours=1),
            artifact="metadata-only-prerequisite-" + suffix + ".joblib",
            metadata_={},
        )
        session.add_all([newest, older_tie, future, model])
        session.flush()
        promotion = Promotion(
            experiment_id=selected.id,
            model_id=model.id,
            created_at=checked_at - timedelta(minutes=1),
            reason="Metadata-only prerequisite selection fixture",
        )
        session.add(promotion)
        session.flush()

    with transaction() as session:
        result = prerequisites.inspect_prerequisites(session, checked_at)
    items = {item.experiment_id: item for item in result.experiments}
    assert set(items) == {selected.id, empty.id}
    assert result.status == "needs_attention" and result.advisory
    assert result.scope_complete and not result.has_more
    observed = items[selected.id]
    assert observed.source.latest_receipt.id == newest.id
    assert observed.source.latest_receipt.received_at == receipt_time
    assert observed.source.latest_receipt.status == "partial"
    assert observed.source.latest_receipt.feed
    assert observed.source.coverage_verified is False
    assert observed.model.kind == "promotion"
    assert observed.model.model_id == model.id and observed.model.promotion_id == promotion.id
    assert observed.model.training_cutoff == model.training_cutoff
    assert observed.model.available_at == model.available_at
    assert items[empty.id].source.status == "no_receipt"
    assert items[empty.id].model.status == "unavailable"
    assert "No eligible model" in items[empty.id].model.message


@pytest.mark.integration
def test_postgis_selected_scope_limits_actual_model_inspection(owned_prerequisites, monkeypatch):
    case = owned_prerequisites
    experiments = [*case.real, *(case.create() for _ in range(19))]
    case.configure([item.id for item in experiments])
    calls = []
    actual_select = prerequisites.select_forecast_model

    def observe_select(session, experiment_id, requested_model_id, cutoff, checked_at):
        calls.append(experiment_id)
        return actual_select(session, experiment_id, requested_model_id, cutoff, checked_at)

    monkeypatch.setattr(prerequisites, "select_forecast_model", observe_select)
    with transaction() as session:
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        result = prerequisites.inspect_prerequisites(session, checked_at)
    expected = [item.id for item in sorted(experiments, key=lambda item: (item.created_at, item.id))[:20]]
    assert [item.experiment_id for item in result.experiments] == expected
    assert calls == expected
    assert result.inspected_count == 20 and result.has_more
    assert result.scope_complete is False
    assert result.status == "needs_attention" and result.advisory
    assert all(item.source.status == "no_receipt" for item in result.experiments)
    assert all(item.model.status == "unavailable" for item in result.experiments)


@pytest.mark.integration
def test_postgis_synthetic_exclusion_and_empty_selected_scope(owned_prerequisites):
    case = owned_prerequisites
    case.configure([], scope="all_real")
    with transaction() as session:
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        result = prerequisites.inspect_prerequisites(session, checked_at)
        reported_ids = [item.experiment_id for item in result.experiments]
        synthetic_ids = list(
            session.scalars(
                select(Experiment.id).where(Experiment.id.in_(reported_ids), Experiment.synthetic == True)
            )
        )
    assert case.synthetic.id not in reported_ids and not synthetic_ids
    assert result.advisory

    case.configure([])
    with transaction() as session:
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        empty = prerequisites.inspect_prerequisites(session, checked_at)
    assert empty.status == "empty_scope"
    assert empty.scope_complete and empty.inspected_count == 0 and empty.experiments == []
