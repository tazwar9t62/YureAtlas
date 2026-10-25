"""Coverage follows receipt-time evidence, including later failed reconciliation."""

from datetime import timedelta
from itertools import permutations
from types import SimpleNamespace
from uuid import uuid4

import pytest
from yureatlas.catalogue import coverage_complete, ensure_experiment, has_coverage, ingest_records
from yureatlas.schemas import ExperimentSpec
from yureatlas.timeutil import utc

START = utc("2020-01-01T00:00:00Z")


def day(value):
    return START + timedelta(days=value)


def attempt(start, end, receipt, status="complete", **report):
    return SimpleNamespace(
        id=f"{start}:{end}:{receipt}:{status}",
        start=day(start),
        end=day(end),
        created_at=day(receipt),
        status=status,
        report=report,
    )


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], False),
        ([attempt(0, 10, 11)], True),
        ([attempt(0, 5, 11), attempt(5, 10, 11)], True),
        ([attempt(0, 4, 11), attempt(5, 10, 11)], False),
        ([attempt(0, 10, 11), attempt(3, 4, 12, "partial")], False),
        ([attempt(0, 10, 11), attempt(3, 4, 12, "failed")], False),
        ([attempt(0, 10, 11), attempt(3, 4, 12, "partial"), attempt(3, 4, 13)], True),
        ([attempt(0, 10, 11), attempt(3, 5, 12, "partial"), attempt(3, 4, 13)], False),
        ([attempt(0, 10, 11), attempt(10, 12, 13, "failed")], True),
        ([attempt(0, 10, 11), attempt(3, 4, 11, "failed")], False),
        ([attempt(0, 10, 11), attempt(0, 10, 12, "partial", feed=True)], True),
        ([attempt(0, 10, 11), attempt(3, 4, 12, "partial", synthetic_revision=True)], True),
        ([attempt(0, 10, 11), attempt(0, 10, 12, "partial", coverage_effect="supplement")], True),
        ([attempt(0, 10, 12, "complete", coverage_effect="supplement")], False),
    ],
)
def test_latest_authoritative_interval_evidence_is_required(runs, expected):
    for order in permutations(runs):
        assert coverage_complete(order, day(0), day(10)) is expected


def test_failed_attempt_only_invalidates_its_half_open_interval():
    runs = [attempt(-2, 12, 13), attempt(3, 5, 14, "failed")]
    assert coverage_complete(runs, day(0), day(3))
    assert not coverage_complete(runs, day(3), day(5))
    assert coverage_complete(runs, day(5), day(10))
    assert not coverage_complete(runs, day(5), day(5))


@pytest.mark.integration
def test_receipt_cutoff_keeps_prior_coverage_and_newer_attempts_can_restore_it():
    experiment = ensure_experiment(
        ExperimentSpec(
            name="coverage_" + uuid4().hex[:12], provider="synthetic", west=140, east=142, south=35, north=37
        )
    )
    for run in [attempt(0, 10, 11), attempt(3, 5, 12, "partial"), attempt(3, 4, 13), attempt(4, 5, 14)]:
        ingest_records(
            experiment.source_id,
            [],
            run.start,
            run.end,
            run.report,
            received_at=run.created_at,
            status=run.status,
        )
    assert has_coverage(experiment.source_id, day(0), day(10), cutoff=day(11))
    assert not has_coverage(experiment.source_id, day(0), day(10), cutoff=day(12))
    assert not has_coverage(experiment.source_id, day(0), day(10), cutoff=day(13))
    assert has_coverage(experiment.source_id, day(0), day(10), cutoff=day(14))
    assert has_coverage(experiment.source_id, day(0), day(10))


@pytest.mark.integration
def test_supplemental_receipts_neither_establish_nor_invalidate_database_coverage():
    experiment = ensure_experiment(
        ExperimentSpec(
            name="coverage_" + uuid4().hex[:12], provider="synthetic", west=140, east=142, south=35, north=37
        )
    )
    ingest_records(
        experiment.source_id,
        [],
        day(0),
        day(10),
        {"coverage_effect": "supplement"},
        received_at=day(11),
        status="partial",
    )
    assert not has_coverage(experiment.source_id, day(0), day(10))
    ingest_records(experiment.source_id, [], day(0), day(10), {}, received_at=day(12))
    ingest_records(
        experiment.source_id, [], day(0), day(10), {"feed": True}, received_at=day(13), status="partial"
    )
    assert has_coverage(experiment.source_id, day(0), day(10))
