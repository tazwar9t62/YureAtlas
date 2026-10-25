"""APScheduler is a trigger only; all decisions and work live in PostgreSQL."""

from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .config import settings
from .db import transaction
from .jobs import enqueue, enqueue_missed
from .tables import Experiment, WorkerState, now
from .timeutil import JST, jst_day, next_day, utc


def real_experiments():
    with transaction() as s:
        return list(s.scalars(select(Experiment).where(Experiment.synthetic == False)))


def ingest_trigger(timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    bucket = int(timestamp.timestamp() // 900)
    for experiment in real_experiments():
        if experiment.config["provider"] == "usgs":
            enqueue(
                "ingest",
                {
                    "experiment_id": experiment.id,
                    "start": (timestamp - timedelta(days=2)).isoformat(),
                    "end": timestamp.isoformat(),
                },
                f"ingest:{experiment.id}:{bucket}",
            )


def daily_forecast_decision(experiment_id, created_at, start, timestamp):
    """Queue the due daily window, or atomically record an absent expired one."""
    if not settings.schedule_enabled:
        return None
    created_at, start, timestamp = utc(created_at), utc(start), utc(timestamp)
    if start != jst_day(start.astimezone(JST).date())[0]:
        raise ValueError("Scheduled daily forecasts require a JST-midnight target start")
    if created_at >= start or timestamp < start - timedelta(minutes=20):
        return None
    payload = {"experiment_id": experiment_id, "start": start.isoformat(), "scheduled": True}
    key = f"daily:{experiment_id}:{start.isoformat()}"
    if timestamp < start:
        return enqueue("forecast", payload, key)
    return enqueue_missed(
        "forecast",
        payload,
        key,
        "Computer/worker unavailable before the prospective publication deadline",
    )


def forecast_trigger(timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    start, _ = next_day(timestamp)
    for experiment in real_experiments():
        daily_forecast_decision(experiment.id, experiment.created_at, start, timestamp)


def evaluate_trigger(reconcile=False, timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    day = timestamp.astimezone(JST).date().isoformat()
    for experiment in real_experiments():
        if reconcile and experiment.config["provider"] == "usgs":
            enqueue(
                "ingest",
                {
                    "experiment_id": experiment.id,
                    "start": (timestamp - timedelta(days=31)).isoformat(),
                    "end": timestamp.isoformat(),
                },
                f"reconcile-ingest:{experiment.id}:{day}",
            )
        enqueue(
            "evaluate",
            {"experiment_id": experiment.id},
            f"{'reconcile' if reconcile else 'evaluate'}:{experiment.id}:{day}",
        )


def training_trigger(timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    for experiment in real_experiments():
        enqueue(
            "train",
            {"experiment_id": experiment.id},
            f"weekly-train:{experiment.id}:{timestamp.astimezone(JST).date().isoformat()}",
        )


def reconcile_startup(timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    with transaction() as s:
        previous = s.get(WorkerState, "scheduler")
        previous_time = utc(previous.heartbeat) if previous else timestamp - timedelta(days=1)
    for experiment in real_experiments():
        cursor = max(previous_time, experiment.created_at).astimezone(JST).date()
        today = timestamp.astimezone(JST).date()
        while cursor <= today:
            start, _ = jst_day(cursor)
            if start <= timestamp:
                daily_forecast_decision(experiment.id, experiment.created_at, start, timestamp)
            cursor += timedelta(days=1)
        # Recover a still-eligible upcoming window independently of the old
        # heartbeat cursor. Before 23:40 this decision deliberately does nothing.
        upcoming, _ = next_day(timestamp)
        daily_forecast_decision(experiment.id, experiment.created_at, upcoming, timestamp)
    # Safe catch-up work has unique keys; never enqueue a backdated forecast.
    ingest_trigger(timestamp)
    evaluate_trigger(reconcile=True, timestamp=timestamp)


def start_scheduler():
    if not settings.schedule_enabled:
        return None
    reconcile_startup()
    scheduler = BackgroundScheduler(
        timezone=JST, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
    )
    scheduler.add_job(ingest_trigger, "cron", minute="*/15", id="ingest")
    scheduler.add_job(forecast_trigger, "cron", hour=23, minute=40, id="forecast")
    scheduler.add_job(evaluate_trigger, "cron", hour=6, minute=0, id="evaluate")
    scheduler.add_job(evaluate_trigger, "cron", hour=6, minute=30, kwargs={"reconcile": True}, id="reconcile")
    scheduler.add_job(training_trigger, "cron", day_of_week="sun", hour=3, minute=0, id="train")
    scheduler.start()
    return scheduler
