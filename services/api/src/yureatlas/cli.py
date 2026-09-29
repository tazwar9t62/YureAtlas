import argparse
import json
import time

from .config import settings
from .db import transaction
from .jobs import enqueue
from .tables import Job, uid
from .timeutil import utc


def main():
    parser = argparse.ArgumentParser(description="YureAtlas durable research commands")
    parser.add_argument(
        "command",
        choices=["demo", "bootstrap-real", "ingest", "train", "forecast", "evaluate", "backtest", "job"],
    )
    parser.add_argument("--experiment-id")
    parser.add_argument("--model-id")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--id")
    parser.add_argument("--fit-days", type=int, default=180)
    parser.add_argument("--tune-days", type=int, default=30)
    parser.add_argument("--calibration-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--folds", type=int, default=1)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument(
        "--inline", action="store_true", help="Explicit native development execution; API always uses worker"
    )
    args = parser.parse_args()
    if args.command == "job":
        with transaction() as s:
            job = s.get(Job, args.id)
            if not job:
                raise SystemExit("Unknown job")
            print(
                json.dumps(
                    {
                        "id": job.id,
                        "status": job.status,
                        "progress": job.progress,
                        "result": job.result,
                        "error": job.error,
                    },
                    indent=2,
                )
            )
        return
    payload = {
        k: v for k, v in vars(args).items() if k not in {"command", "wait", "inline", "id"} and v is not None
    }
    for key in ("start", "end"):
        if key in payload:
            value = payload[key]
            payload[key] = utc(value if "T" in value else value + "T00:00:00Z").isoformat()
    kind = args.command
    if kind == "bootstrap-real":
        kind = "ingest"
        payload["start"] = payload.get("start", settings.history_start + "T00:00:00Z")
    if args.inline:
        from .tasks import execute

        print(json.dumps(execute(kind, payload, lambda p: print(f"progress={p:.0%}", flush=True)), indent=2))
        return
    key = "demo:synthetic-seed-v1" if kind == "demo" else f"cli:{kind}:{uid()}"
    job = enqueue(kind, payload, key)
    print(json.dumps({"job_id": job.id, "status": job.status}), flush=True)
    if args.wait:
        while True:
            with transaction() as s:
                job = s.get(Job, job.id)
                if job.status not in {"queued", "running"}:
                    print(
                        json.dumps({"status": job.status, "result": job.result, "error": job.error}, indent=2)
                    )
                    if job.status != "completed":
                        raise SystemExit(1)
                    return
            time.sleep(2)


if __name__ == "__main__":
    main()
