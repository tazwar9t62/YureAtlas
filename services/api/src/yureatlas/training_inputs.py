"""Validate chronological job inputs before creating archive evidence."""

from datetime import timedelta

from .timeutil import JST, jst_day, utc

# Match JobRequest's public bounds; direct and CLI calls do not pass through it.
DAY_LIMITS = {
    "fit_days": (30, 730),
    "tune_days": (7, 120),
    "calibration_days": (7, 120),
    "test_days": (1, 180),
    "folds": (1, 6),
}


def _validate_days(**values):
    for name, value in values.items():
        minimum, maximum = DAY_LIMITS[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def validate_training_inputs(cutoff, timestamp, fit_days, tune_days, calibration_days):
    _validate_days(fit_days=fit_days, tune_days=tune_days, calibration_days=calibration_days)
    cutoff = jst_day(utc(cutoff).astimezone(JST).date())[0]
    if cutoff > utc(timestamp):
        raise ValueError("Training target windows must be completed")
    return cutoff


def validate_backtest_inputs(start, timestamp, test_days, folds, fit_days, tune_days, calibration_days):
    _validate_days(
        fit_days=fit_days,
        tune_days=tune_days,
        calibration_days=calibration_days,
        test_days=test_days,
        folds=folds,
    )
    start = utc(start)
    if start + timedelta(days=test_days * folds) > utc(timestamp):
        raise ValueError("Backtest target windows must be completed")
    return start
