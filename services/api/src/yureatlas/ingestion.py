import sys
from datetime import timedelta

import httpx
from sqlalchemy import select

from .catalogue import discovery_and_corrections, ensure_experiment, ingest_records, load_experiment
from .db import transaction
from .jobs import Cancelled
from .providers import USGS, RetrievalBudgetExceeded
from .schemas import ExperimentSpec
from .tables import Event, Revision, now
from .timeutil import utc

KNOWN_ID_LIMIT = 10_000


def known_ids_for_interval(source_id, start, end, *, limit=KNOWN_ID_LIMIT):
    """Bound the read, but include IDs whose *older* versions occupied the interval."""
    with transaction() as s:
        ids = list(
            s.scalars(
                select(Event.provider_id)
                .where(
                    Event.source_id == source_id,
                    select(Revision.id)
                    .where(
                        Revision.event_id == Event.id,
                        Revision.origin_time >= start,
                        Revision.origin_time < end,
                    )
                    .exists(),
                )
                .order_by(Event.provider_id)
                .limit(limit + 1)
            )
        )
    return ids[:limit], len(ids) > limit


def ingest_usgs(experiment_id, start, end, progress=lambda p: None, use_feed=False):
    experiment, grid = load_experiment(experiment_id)
    if experiment.config["provider"] != "usgs":
        raise ValueError("USGS refresh only accepts a USGS experiment")
    if end > now() or start >= end:
        raise ValueError("Ingestion requires a completed ordered interval")
    fraction = 0.0
    provider = USGS(progress=lambda: progress(fraction))
    totals = {
        "added": 0,
        "duplicate": 0,
        "chunks": 0,
        "rejected": 0,
        "status": "complete",
        "pending_ids_at_least": 0,
        "failed_ids": 0,
    }

    def persist(records, a, b, report, *, status="partial", received_at=None):
        result = ingest_records(
            experiment.source_id, records, a, b, report, status=status, received_at=received_at
        )
        totals["added"] += result["added"]
        totals["duplicate"] += result["duplicate"]

    cursor = start
    try:
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=30), end)
            provider.requests.clear()
            # An interrupted attempt must not leave an older complete query
            # silently covering newly attempted, unfinished reconciliation.
            persist(
                [],
                cursor,
                chunk_end,
                {
                    "coverage_effect": "authoritative",
                    "phase": "started",
                    "reason": "Discovery and known-ID reconciliation are pending",
                },
            )
            try:
                known, overflow = known_ids_for_interval(experiment.source_id, cursor, chunk_end)
                known = set(known)
                resolved = set()
                rejected = 0
                failures = []
                pending = []
                progress(fraction)
                if use_feed and cursor == start:
                    first_request = len(provider.requests)
                    try:
                        records, report = provider.feed()
                    except Cancelled:
                        raise
                    except (httpx.HTTPError, ValueError) as exc:
                        persist(
                            [],
                            cursor,
                            chunk_end,
                            {
                                "coverage_effect": "supplement",
                                "feed": True,
                                "error": str(exc),
                                "requests": provider.requests[first_request:],
                            },
                            status="failed",
                        )
                    else:
                        records = discovery_and_corrections(
                            experiment.source_id, records, grid, experiment.config
                        )
                        persist(
                            records,
                            cursor,
                            chunk_end,
                            dict(report, coverage_effect="supplement"),
                            received_at=utc(report["received_at"]),
                        )
                        # Newly persisted feed events can also become stale before
                        # discovery. Include in-interval IDs, without claiming that
                        # a feed resolved them or growing the bounded working set.
                        for record in records:
                            if cursor <= record["origin_time"] < chunk_end:
                                identity = record["provider_id"]
                                if identity in known or len(known) < KNOWN_ID_LIMIT:
                                    known.add(identity)
                                else:
                                    overflow = True
                retrieved_to = cursor
                for a, b, records, report in provider.retrieve(
                    cursor, chunk_end, grid.spec, experiment.config["input_magnitude"]
                ):
                    if a != retrieved_to or not a < b <= chunk_end:
                        raise ValueError("Discovery intervals are incomplete, overlapping, or out of order")
                    rejected += report["rejected_count"]
                    resolved.update(r["provider_id"] for r in records if r["provider_id"] in known)
                    records = discovery_and_corrections(
                        experiment.source_id, records, grid, experiment.config
                    )
                    persist(
                        records,
                        a,
                        b,
                        dict(report, coverage_effect="supplement"),
                        received_at=utc(report["received_at"]),
                    )
                    retrieved_to = b
                    fraction = float(((cursor - start) + (b - cursor) * 0.7) / (end - start))
                    progress(fraction)
                if retrieved_to != chunk_end:
                    raise ValueError("Discovery did not retrieve the complete requested interval")
                missing = sorted(known - resolved)
                for index, provider_id in enumerate(missing):
                    first_request = len(provider.requests)
                    try:
                        correction, report = provider.retrieve_id(provider_id)
                    except Cancelled:
                        raise
                    except RetrievalBudgetExceeded as exc:
                        pending = missing[index:]
                        failures.append(
                            {
                                "id": provider_id,
                                "error": str(exc),
                                "budget": True,
                                "requests": provider.requests[first_request:],
                            }
                        )
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        failures.append(
                            {
                                "id": provider_id,
                                "error": str(exc),
                                "requests": provider.requests[first_request:],
                            }
                        )
                    else:
                        persist(
                            [correction],
                            cursor,
                            chunk_end,
                            dict(report, coverage_effect="supplement", known_id=provider_id),
                            received_at=utc(report["received_at"]),
                        )
                        resolved.add(provider_id)
                    fraction = float(
                        (
                            (cursor - start)
                            + (chunk_end - cursor) * (0.7 + 0.3 * (index + 1) / max(1, len(missing)))
                        )
                        / (end - start)
                    )
                    progress(fraction)
                unresolved = known - resolved
                status = "partial" if rejected or unresolved or overflow else "complete"
                report = {
                    "coverage_effect": "authoritative",
                    "phase": "finished",
                    "coverage_policy": "complete discovery plus all bounded archived and feed IDs resolved",
                    "known_ids": len(known),
                    "known_id_limit": KNOWN_ID_LIMIT,
                    "known_id_overflow": overflow,
                    "resolved_known_ids": len(resolved),
                    "pending_ids_at_least": len(pending) + int(overflow),
                    "pending_id_sample": pending[:100],
                    "unresolved_id_sample": sorted(unresolved)[:100],
                    "failed_ids": sum(not failure.get("budget") for failure in failures),
                    "failures": failures[:100],
                    "rejected_count": rejected,
                    "requests": provider.requests[:],
                    "limits": provider.budget_report(),
                    "next_action": "Retry a narrower interval" if pending or overflow else None,
                }
                persist([], cursor, chunk_end, report, status=status)
                totals["chunks"] += 1
                totals["rejected"] += rejected
                totals["pending_ids_at_least"] += report["pending_ids_at_least"]
                totals["failed_ids"] += report["failed_ids"]
                if status != "complete":
                    totals["status"] = "partial"
            except Exception as exc:
                try:
                    persist(
                        [],
                        cursor,
                        chunk_end,
                        {
                            "coverage_effect": "authoritative",
                            "phase": "interrupted",
                            "error": str(exc),
                            "requests": provider.requests[:],
                            "limits": provider.budget_report(),
                        },
                        status="partial" if isinstance(exc, Cancelled) else "failed",
                    )
                except Exception as recording_error:  # noqa: BLE001 - preserve the primary failure
                    exc.add_note(f"Could not record failed ingestion: {type(recording_error).__name__}")
                raise
            cursor = chunk_end
            fraction = float((cursor - start) / (end - start))
            progress(fraction)
        totals["limits"] = provider.budget_report()
        return totals
    finally:
        primary_error = sys.exception()
        try:
            provider.close()
        except Exception as closing_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"Could not close USGS provider: {type(closing_error).__name__}")


def real_experiment():
    return ensure_experiment(ExperimentSpec(name="usgs_preferred_m4_v1"))
