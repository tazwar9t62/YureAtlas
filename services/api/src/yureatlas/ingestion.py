from datetime import timedelta

from .catalogue import ensure_experiment, ingest_records, load_experiment
from .db import transaction
from .providers import USGS
from .schemas import ExperimentSpec
from .tables import Ingestion, Source, now


def ingest_usgs(experiment_id, start, end, progress=lambda p: None, use_feed=False):
    experiment, grid = load_experiment(experiment_id)
    if experiment.config["provider"] != "usgs":
        raise ValueError("USGS refresh only accepts a USGS experiment")
    if end > now() or start >= end:
        raise ValueError("Ingestion requires a completed ordered interval")
    provider = USGS()
    totals = {"added": 0, "duplicate": 0, "chunks": 0, "rejected": 0}
    if use_feed:
        (records, rejected), raw = provider.feed()
        records = [r for r in records if grid.assign(r["longitude"], r["latitude"]) is not None
                   and r["magnitude"] >= experiment.config["input_magnitude"]]
        ingest_records(experiment.source_id, records, end-timedelta(days=1), end,
                       {"feed": True, "raw_ref": raw, "rejected": rejected[:100]}, status="partial")
    cursor = start
    while cursor < end:
        chunk_end = min(cursor+timedelta(days=30), end)
        try:
            for a, b, records, report in provider.retrieve(cursor, chunk_end, grid.spec, experiment.config["input_magnitude"]):
                # Invalid records mean usable negatives cannot be guaranteed.
                status = "complete" if not report["rejected_count"] else "partial"
                result = ingest_records(experiment.source_id, records, a, b, report, status=status)
                totals["added"] += result["added"]
                totals["duplicate"] += result["duplicate"]
                totals["rejected"] += report["rejected_count"]
                totals["chunks"] += 1
            cursor = chunk_end
            progress((cursor-start)/(end-start))
        except Exception as exc:
            with transaction() as s:
                s.add(Ingestion(source_id=experiment.source_id, start=cursor, end=chunk_end, status="failed",
                                report={"error": str(exc), "received_at": now().isoformat()}))
                source = s.get(Source, experiment.source_id)
                source.status = "failed"
                source.details = dict(source.details, error=str(exc))
            raise
    return totals


def real_experiment():
    return ensure_experiment(ExperimentSpec(name="usgs_preferred_m4_v1"))
