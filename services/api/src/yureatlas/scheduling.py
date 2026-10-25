"""Bounded durable schedule decisions; monotonic waits only wake the planner."""

import logging
import threading
from collections import Counter
from contextlib import nullcontext
from datetime import timedelta

from sqlalchemy import select, text

from .config import settings
from .db import transaction
from .execution import WorkerOwnershipLost, execution_scope
from .jobs import enqueue, enqueue_batch, enqueue_missed
from .tables import Experiment, SchedulerState, now
from .timeutil import JST, jst_day, next_day, utc

log = logging.getLogger("yureatlas.scheduling")
STATE_ID = "default"
RECONCILE_SECONDS = 30
MAX_EXPIRED_TARGETS = 31
MAX_REAL_EXPERIMENTS = 100
POLICY_VERSION = "jst-latest-due-v1"
MISSED_REASON = "No recorded on-time scheduling decision before the prospective publication deadline"


def real_experiments(*, session=None):
    if session is None:
        with transaction() as owned:
            return real_experiments(session=owned)
    experiments = list(
        session.scalars(
            select(Experiment)
            .where(Experiment.synthetic == False)
            .order_by(Experiment.created_at, Experiment.id)
            .limit(MAX_REAL_EXPERIMENTS + 1)
        )
    )
    if len(experiments) > MAX_REAL_EXPERIMENTS:
        raise ValueError(
            f"Automatic scheduling supports at most {MAX_REAL_EXPERIMENTS} real experiments; "
            "no jobs or cursor were changed. Disable scheduling and review the experiment scope."
        )
    return experiments


def _daily_plan(experiment_id, created_at, start, timestamp):
    created_at, start, timestamp = utc(created_at), utc(start), utc(timestamp)
    if start != jst_day(start.astimezone(JST).date())[0]:
        raise ValueError("Scheduled daily forecasts require a JST-midnight target start")
    if created_at >= start or timestamp < start - timedelta(minutes=20):
        return None
    plan = {
        "kind": "forecast",
        "payload": {"experiment_id": experiment_id, "start": start.isoformat(), "scheduled": True},
        "key": f"daily:{experiment_id}:{start.isoformat()}",
    }
    if timestamp >= start:
        plan["missed_reason"] = MISSED_REASON
    return plan


def daily_forecast_decision(experiment_id, created_at, start, timestamp, *, session=None):
    """Queue the due daily window, or atomically record an absent expired one."""
    if not settings.schedule_enabled:
        return None
    plan = _daily_plan(experiment_id, created_at, start, timestamp)
    if plan is None:
        return None
    options = {"session": session} if session is not None else {}
    if "missed_reason" in plan:
        return enqueue_missed(plan["kind"], plan["payload"], plan["key"], plan["missed_reason"], **options)
    return enqueue(plan["kind"], plan["payload"], plan["key"], **options)


def latest_due(timestamp):
    """Calculate canonical occurrences in constant time, without replaying ticks."""
    timestamp = utc(timestamp)
    today, _ = jst_day(timestamp.astimezone(JST).date())

    def daily(hour, minute=0):
        due = today + timedelta(hours=hour, minutes=minute)
        return due if due <= timestamp else due - timedelta(days=1)

    local_day = timestamp.astimezone(JST).date()
    sunday = local_day - timedelta(days=(local_day.weekday() + 1) % 7)
    weekly = jst_day(sunday)[0] + timedelta(hours=3)
    if weekly > timestamp:
        weekly -= timedelta(days=7)
    quarter = timestamp.replace(minute=timestamp.minute // 15 * 15, second=0, microsecond=0)
    return {"ingest": quarter, "evaluate": daily(6), "reconcile": daily(6, 30), "train": weekly}


def _safe_plans(experiment, due):
    plans = []
    for kind, occurrence in due.items():
        if occurrence < utc(experiment.created_at):
            continue
        if kind == "ingest" and experiment.config["provider"] != "usgs":
            continue
        payload = {
            "experiment_id": experiment.id,
            "scheduled": True,
            "scheduled_for": occurrence.isoformat(),
        }
        day = occurrence.astimezone(JST).date().isoformat()
        if kind == "ingest":
            payload.update(start=(occurrence - timedelta(days=2)).isoformat(), end=occurrence.isoformat())
            key = f"ingest:{experiment.id}:{int(occurrence.timestamp() // 900)}"
        elif kind == "reconcile":
            # One durable task performs ingestion before reevaluation. Its old
            # reconcile key remains authoritative if a legacy job already exists.
            payload.update(start=(occurrence - timedelta(days=31)).isoformat(), end=occurrence.isoformat())
            key = f"reconcile:{experiment.id}:{day}"
        elif kind == "train":
            payload["end"] = occurrence.isoformat()
            key = f"weekly-train:{experiment.id}:{day}"
        else:
            key = f"evaluate:{experiment.id}:{day}"
        plans.append({"kind": kind, "payload": payload, "key": key})
    return plans


def _safe_trigger(kind, timestamp=None):
    if not settings.schedule_enabled:
        return None
    timestamp = utc(timestamp if timestamp is not None else now())
    due = latest_due(timestamp)
    with transaction() as session:
        plans = [
            plan
            for experiment in real_experiments(session=session)
            for plan in _safe_plans(experiment, {kind: due[kind]})
        ]
        return enqueue_batch(plans, session=session)


def ingest_trigger(timestamp=None):
    return _safe_trigger("ingest", timestamp)


def forecast_trigger(timestamp=None):
    if not settings.schedule_enabled:
        return
    timestamp = utc(timestamp if timestamp is not None else now())
    start, _ = next_day(timestamp)
    with transaction() as session:
        for experiment in real_experiments(session=session):
            daily_forecast_decision(experiment.id, experiment.created_at, start, timestamp, session=session)


def evaluate_trigger(reconcile=False, timestamp=None):
    return _safe_trigger("reconcile" if reconcile else "evaluate", timestamp)


def training_trigger(timestamp=None):
    return _safe_trigger("train", timestamp)


def reconcile(timestamp=None):
    """Commit bounded scheduling decisions and their independent cursor together.

    The high watermark never moves backward. The previous *observed* clock is
    still used to recover elapsed windows after a backward jump and later sleep.
    Disabled scheduling leaves both jobs and this state unchanged.
    """
    if not settings.schedule_enabled:
        return {"status": "disabled"}
    requested_timestamp = utc(timestamp) if timestamp is not None else None
    with transaction() as session:
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(text("SELECT pg_advisory_xact_lock(8247714, 1)"))
        # Default decisions use the clock after any lock wait. A captured time
        # from before midnight must not queue an already expired target, or make
        # contention between callers look like a backwards wall-clock jump.
        timestamp = requested_timestamp if requested_timestamp is not None else utc(now())
        today, _ = jst_day(timestamp.astimezone(JST).date())
        # Exactly 31 expired midnight targets, including today's, plus the upcoming
        # target. Older windows are summarized explicitly, never silently replayed.
        retained_from = today - timedelta(days=MAX_EXPIRED_TARGETS - 1)
        due = latest_due(timestamp)
        experiments = real_experiments(session=session)
        state = session.get(SchedulerState, STATE_ID)
        previous_details = state.details if state else {}
        previous_observed = utc(state.observed_at) if state else None
        previous_high = utc(state.reconciled_at) if state else timestamp
        regressed = previous_observed is not None and timestamp < previous_observed
        # A backward clock check considers only bounded recent/current work.
        # It does not invent an elapsed interval extending into the future.
        after = timestamp - timedelta(days=MAX_EXPIRED_TARGETS) if regressed else previous_observed
        plans = {}
        omitted = []
        for experiment in experiments:
            created = utc(experiment.created_at)
            requested_after = max(created, after) if after is not None else created
            first, _ = next_day(requested_after)
            if first < retained_from:
                omitted.append(
                    {
                        "experiment_id": experiment.id,
                        "from": first.isoformat(),
                        "before": retained_from.isoformat(),
                        "target_count": (retained_from - first).days,
                    }
                )
            cursor = max(first, retained_from)
            while cursor <= today:
                plan = _daily_plan(experiment.id, created, cursor, timestamp)
                if plan:
                    plans[plan["key"]] = plan
                cursor += timedelta(days=1)
            # Current and upcoming decisions never depend on cursor catch-up.
            for start in (today, today + timedelta(days=1)):
                plan = _daily_plan(experiment.id, created, start, timestamp)
                if plan:
                    plans[plan["key"]] = plan
            for plan in _safe_plans(experiment, due):
                plans[plan["key"]] = plan
        last_truncation = previous_details.get("last_truncation")
        if omitted:
            last_truncation = {
                "observed_at": timestamp.isoformat(),
                "retained_from": retained_from.isoformat(),
                "omitted_target_count": sum(item["target_count"] for item in omitted),
                "experiment_intervals": omitted,
                "meaning": "Older target starts were not examined by this recovery pass; existing jobs remain unchanged",
            }
        rows = list(plans.values())
        inserted = enqueue_batch(rows, session=session)
        high = max(previous_high, timestamp)
        details = {
            "status": "reconciled",
            "policy_version": POLICY_VERSION,
            "timezone": "Asia/Tokyo",
            "observed_at": timestamp.isoformat(),
            "reconciled_at": high.isoformat(),
            "clock_regressed": regressed,
            "clock_below_watermark": timestamp < previous_high,
            "experiment_count": len(experiments),
            "planned_jobs": len(rows),
            "inserted_jobs": len(inserted),
            "existing_jobs": len(rows) - len(inserted),
            "planned_by_kind": dict(Counter(plan["kind"] for plan in rows)),
            "planned_missed_forecasts": sum("missed_reason" in plan for plan in rows),
            "last_truncation": last_truncation,
            "limits": {
                "expired_targets_per_experiment": MAX_EXPIRED_TARGETS,
                "real_experiments": MAX_REAL_EXPERIMENTS,
            },
            "safe_catchup_policy": "Only latest due occurrence per kind; older safe occurrences are coalesced, including weekly candidate training; no auto-promotion",
        }
        if state is None:
            state = SchedulerState(id=STATE_ID)
            session.add(state)
        state.reconciled_at = high
        state.observed_at = timestamp
        state.details = details
        session.flush()
        return details


def reconcile_startup(timestamp=None):
    if not settings.schedule_enabled:
        return None
    return reconcile(timestamp)


class ReconciliationWatchdog:
    """A monotonic wait wakes wall-clock reconciliation even after clock changes."""

    def __init__(self, owner=None):
        self._owner = owner
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="yureatlas-scheduler", daemon=True)

    def _check(self):
        try:
            with execution_scope(self._owner) if self._owner is not None else nullcontext():
                reconcile()
        except WorkerOwnershipLost:
            self._stop.set()
            log.exception("Schedule reconciliation stopped because worker ownership was lost")
        except Exception:
            log.exception("Schedule reconciliation failed; durable cursor unchanged")

    def _run(self):
        while not self._stop.wait(RECONCILE_SECONDS):
            self._check()

    def start(self):
        # A transient initial database failure is degraded scheduling, not a
        # successful pass: log it and let later monotonic wakes retry.
        self._check()
        self._thread.start()
        return self

    def shutdown(self, wait=True):
        self._stop.set()
        if wait and threading.current_thread() is not self._thread:
            self._thread.join()


def start_scheduler(owner=None):
    if not settings.schedule_enabled:
        return None
    return ReconciliationWatchdog(owner).start()
