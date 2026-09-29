"""APScheduler is a trigger only; all decisions and work live in PostgreSQL."""

from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .config import settings
from .db import transaction
from .jobs import enqueue, finish
from .tables import Experiment, Job, WorkerState, now
from .timeutil import JST, jst_day, next_day


def real_experiments():
    with transaction() as s:
        return list(s.scalars(select(Experiment).where(Experiment.synthetic == False)))


def ingest_trigger():
    timestamp = now()
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


def forecast_trigger():
    start, _ = next_day(now())
    for experiment in real_experiments():
        enqueue(
            "forecast",
            {"experiment_id": experiment.id, "start": start.isoformat(), "scheduled": True},
            f"daily:{experiment.id}:{start.isoformat()}",
        )


def evaluate_trigger(reconcile=False):
    day = now().astimezone(JST).date().isoformat()
    for experiment in real_experiments():
        if reconcile and experiment.config["provider"] == "usgs":
            timestamp = now()
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


def training_trigger():
    for experiment in real_experiments():
        enqueue(
            "train",
            {"experiment_id": experiment.id},
            f"weekly-train:{experiment.id}:{now().astimezone(JST).date().isoformat()}",
        )


def reconcile_startup(timestamp=None):
    timestamp = timestamp or now()
    with transaction() as s:
        previous = s.get(WorkerState, "scheduler")
        previous_time = previous.heartbeat if previous else timestamp - timedelta(days=1)
    for experiment in real_experiments():
        cursor = max(previous_time, experiment.created_at).astimezone(JST).date()
        today = timestamp.astimezone(JST).date()
        while cursor <= today:
            start, _ = jst_day(cursor)
            if experiment.created_at < start <= timestamp:
                key = f"daily:{experiment.id}:{start.isoformat()}"
                with transaction() as s:
                    exists = s.scalar(select(Job).where(Job.logical_key == key))
                if not exists:
                    job = enqueue(
                        "forecast",
                        {"experiment_id": experiment.id, "start": start.isoformat(), "scheduled": True},
                        key,
                    )
                    finish(
                        job.id,
                        "missed",
                        error="Computer/worker unavailable before the prospective publication deadline",
                    )
            cursor += timedelta(days=1)
    # Safe catch-up work has unique keys; never enqueue a backdated forecast.
    ingest_trigger()
    evaluate_trigger(reconcile=True)


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
