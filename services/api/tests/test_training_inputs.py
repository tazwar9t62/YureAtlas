"""Invalid direct/CLI chronology fails before any experiment or archive work."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from yureatlas import learning, tasks
from yureatlas.schemas import JobRequest
from yureatlas.timeutil import JST, utc
from yureatlas.training_inputs import validate_backtest_inputs, validate_training_inputs

NOW = utc("2025-01-02T15:00:00Z")
PAST = utc("2024-01-01T15:00:00Z")
PARTITIONS = {"fit_days": 30, "tune_days": 7, "calibration_days": 7}


@pytest.fixture
def no_archive_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid inputs reached experiment/archive work")

    monkeypatch.setattr(learning, "now", lambda: NOW)
    monkeypatch.setattr(tasks, "now", lambda: NOW)
    for module, names in (
        (learning, ("load_experiment", "make_snapshot", "put", "save_model")),
        (tasks, ("load_experiment", "real_experiment")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize("field", ["fit_days", "tune_days", "calibration_days"])
@pytest.mark.parametrize("value", [0, -1, 731, 30.0, True, "30", None])
def test_training_partition_inputs_reject_before_direct_or_cli_archive_work(no_archive_work, field, value):
    partitions = dict(PARTITIONS, **{field: value})
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        learning.train_suite("experiment", PAST, **partitions)
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        tasks.execute("train", {"end": PAST.isoformat(), **partitions}, lambda _: None)


@pytest.mark.parametrize("field", ["fit_days", "tune_days", "calibration_days", "test_days", "folds"])
@pytest.mark.parametrize("value", [0, -1, 731, 1.0, True, "1", None])
def test_backtest_partition_inputs_reject_before_direct_or_cli_archive_work(no_archive_work, field, value):
    options = dict(PARTITIONS, test_days=2, folds=2)
    options[field] = value
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        tasks.run_backtest("experiment", PAST, **options)
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        tasks.execute("backtest", {"start": PAST.isoformat(), **options}, lambda _: None)


@pytest.mark.parametrize(
    "field, minimum, maximum",
    [
        ("fit_days", 30, 730),
        ("tune_days", 7, 120),
        ("calibration_days", 7, 120),
        ("test_days", 1, 180),
        ("folds", 1, 6),
    ],
)
def test_direct_and_public_api_partition_limits_agree(field, minimum, maximum):
    for value in (minimum, maximum):
        options = dict(PARTITIONS, test_days=1, folds=1)
        options[field] = value
        # Give even the largest permitted fold/test span enough completed time.
        end = PAST + timedelta(days=options["test_days"] * options["folds"])
        assert validate_backtest_inputs(PAST, end, **options) == PAST
        assert getattr(JobRequest(**options), field) == value
        if field in PARTITIONS:
            partitions = dict(PARTITIONS, **{field: value})
            assert validate_training_inputs(PAST, PAST, **partitions) == PAST
    for value in (minimum - 1, maximum + 1):
        options = dict(PARTITIONS, test_days=1, folds=1)
        options[field] = value
        with pytest.raises(ValueError, match=field):
            validate_backtest_inputs(PAST, NOW, **options)
        with pytest.raises(ValidationError):
            JobRequest(**options)


@pytest.mark.parametrize("kind", ["train", "backtest"])
def test_naive_times_reject_before_direct_or_cli_archive_work(no_archive_work, kind):
    naive = PAST.replace(tzinfo=None)
    with pytest.raises(ValueError, match="Timestamp must include a timezone"):
        if kind == "train":
            learning.train_suite("experiment", naive, **PARTITIONS)
        else:
            tasks.run_backtest("experiment", naive, **PARTITIONS)
    with pytest.raises(ValueError, match="Timestamp must include a timezone"):
        tasks.execute(kind, {"end" if kind == "train" else "start": naive.isoformat()}, lambda _: None)


def test_training_floor_uses_the_jst_date_and_accepts_the_exact_completed_boundary():
    # A later requested time on the same local date still resolves to midnight.
    requested = NOW + timedelta(hours=12)
    assert validate_training_inputs(requested, NOW, **PARTITIONS) == NOW
    assert validate_training_inputs(requested.astimezone(JST), NOW, **PARTITIONS) == NOW
    assert validate_training_inputs(NOW, NOW, **PARTITIONS) == NOW
    with pytest.raises(ValueError, match="Training target windows must be completed"):
        validate_training_inputs(NOW, NOW - timedelta(microseconds=1), **PARTITIONS)


def test_future_resolved_training_cutoff_rejects_before_archive_work(no_archive_work):
    future = NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="Training target windows must be completed"):
        learning.train_suite("experiment", future, **PARTITIONS)
    with pytest.raises(ValueError, match="Training target windows must be completed"):
        tasks.execute("train", {"end": future.isoformat(), **PARTITIONS}, lambda _: None)


def test_backtest_preserves_manual_windows_and_accepts_exact_completion():
    start = PAST + timedelta(hours=5, minutes=13)
    end = start + timedelta(days=6)
    assert validate_backtest_inputs(start.astimezone(JST), end, 2, 3, **PARTITIONS) == start
    with pytest.raises(ValueError, match="Backtest target windows must be completed"):
        validate_backtest_inputs(start, end - timedelta(microseconds=1), 2, 3, **PARTITIONS)


def test_unfinished_backtest_and_absent_start_reject_before_archive_work(no_archive_work):
    start = NOW - timedelta(days=2) + timedelta(microseconds=1)
    with pytest.raises(ValueError, match="Backtest target windows must be completed"):
        tasks.run_backtest("experiment", start, test_days=2, **PARTITIONS)
    with pytest.raises(ValueError, match="Backtest target windows must be completed"):
        tasks.execute("backtest", {"start": start.isoformat(), "test_days": 2}, lambda _: None)
    with pytest.raises(ValueError, match="explicit first test-window start"):
        tasks.execute("backtest", {}, lambda _: None)
