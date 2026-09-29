"""Artificial clustered events, deterministic and source-isolated."""

from datetime import timedelta

import numpy as np
from sqlalchemy import select

from .catalogue import ensure_experiment, ingest_records
from .db import transaction
from .schemas import ExperimentSpec
from .tables import Ingestion
from .timeutil import utc

DEMO_START = utc("2023-01-01T15:00:00Z")
DEMO_TEST_START = utc("2024-01-15T15:00:00Z")
DEMO_END = DEMO_TEST_START + timedelta(days=10)


def synthetic_records():
    rng = np.random.default_rng(2026)
    centers = np.array([[142.2, 38.4], [140.8, 35.7], [143.8, 42.4], [132.4, 31.6], [137.4, 36.8]])
    records = []
    for day in range((DEMO_END - DEMO_START).days):
        # These are synthetic regimes for software validation, not real sequences.
        for _ in range(rng.poisson(15 + 12 * (day % 37 < 6))):
            center = centers[rng.integers(len(centers))]
            lon, lat = center + rng.normal(0, [0.58, 0.36])
            origin = DEMO_START + timedelta(days=day, seconds=float(rng.uniform(0, 86400)))
            records.append(
                {
                    "provider_id": f"demo-{len(records):07d}",
                    "origin_time": origin,
                    "provider_updated": origin + timedelta(minutes=10),
                    "longitude": float(lon),
                    "latitude": float(lat),
                    "depth_km": None if len(records) % 31 == 0 else float(rng.gamma(2, 18)),
                    "magnitude": float(3 + rng.exponential(0.9)),
                    "magnitude_type": "synthetic",
                    "status": "reviewed",
                }
            )
        for _ in range(rng.poisson(2)):
            origin = DEMO_START + timedelta(days=day, seconds=float(rng.uniform(0, 86400)))
            records.append(
                {
                    "provider_id": f"demo-{len(records):07d}",
                    "origin_time": origin,
                    "provider_updated": origin,
                    "longitude": float(rng.uniform(122.01, 155.99)),
                    "latitude": float(rng.uniform(20.01, 47.99)),
                    "depth_km": 20.0,
                    "magnitude": float(3 + rng.exponential(0.7)),
                    "magnitude_type": "synthetic",
                    "status": "reviewed",
                }
            )
    return records


def seed_catalogue(progress=lambda p: None):
    experiment = ensure_experiment(ExperimentSpec(name="synthetic_japan_m4_v1", provider="synthetic"))
    with transaction() as s:
        if s.scalar(
            select(Ingestion.id).where(
                Ingestion.source_id == experiment.source_id,
                Ingestion.report["seed_complete"].as_boolean() == True,
            )
        ):
            return experiment
    records = synthetic_records()
    # Chunk receipts by day. One gap in the test period remains unknown, even
    # though other deliveries may contain some events in that interval.
    total_days = (DEMO_END - DEMO_START).days
    for day in range(total_days):
        start, end = DEMO_START + timedelta(days=day), DEMO_START + timedelta(days=day + 1)
        selected = [r for r in records if start <= r["origin_time"] < end]
        gap = start == DEMO_TEST_START + timedelta(days=2)
        ingest_records(
            experiment.source_id,
            selected + selected[:1],
            start,
            end,
            {
                "synthetic": True,
                "seed": 2026,
                "gap_explanation": "artificial retrieval outage" if gap else None,
            },
            received_at=end + timedelta(hours=1),
            status="failed" if gap else "complete",
        )
        if day % 20 == 0:
            progress((day + 1) / total_days * 0.8)
    # Actual append-only magnitude/location revision plus an explicit deletion.
    for idx, event in enumerate(records[-60:-57]):
        changed = dict(
            event,
            magnitude=event["magnitude"] + 0.8,
            longitude=event["longitude"] + 0.4,
            provider_updated=event["origin_time"] + timedelta(days=2),
            status="deleted" if idx == 2 else "reviewed",
        )
        ingest_records(
            experiment.source_id,
            [changed],
            changed["origin_time"],
            changed["origin_time"] + timedelta(seconds=1),
            {"synthetic_revision": True},
            received_at=DEMO_END + timedelta(days=3),
            status="partial",
        )
    # Marker has no interval coverage. It must not paper over the deliberate gap.
    ingest_records(
        experiment.source_id,
        [],
        DEMO_END,
        DEMO_END,
        {"seed_complete": True},
        received_at=DEMO_END + timedelta(days=4),
        status="partial",
    )
    progress(1)
    return experiment
