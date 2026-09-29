import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from .db import engine, transaction
from .jobs import Cancelled, checkpoint, claim, finish
from .tables import WorkerState, now

log = logging.getLogger("yureatlas.worker")


def dispatch(kind, payload, progress):
    if kind == "ping":
        return {"message": "Worker is ready"}
    from .tasks import execute
    return execute(kind, payload, progress)


def run_once() -> bool:
    job = claim()
    if not job:
        return False
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(15):
            try:
                checkpoint(job.id)
            except Cancelled:
                return
            except Exception:
                log.exception("Heartbeat failed")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(heartbeat)
        try:
            result = dispatch(job.kind, job.payload, lambda p: checkpoint(job.id, p))
            finish(job.id, "completed", result)
        except Cancelled as exc:
            finish(job.id, "cancelled", error=str(exc))
        except Exception as exc:
            log.exception("Job %s failed", job.id)
            finish(job.id, "failed", error=str(exc)[:4000])
        finally:
            stop.set()
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # A session-level advisory lock enforces one worker/scheduler per database.
    with engine.connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(8247711)")):
            raise RuntimeError("A YureAtlas worker is already running")
        while True:
            with transaction() as s:
                s.execute(insert(WorkerState).values(id="scheduler", heartbeat=now(), details={})
                          .on_conflict_do_update(index_elements=[WorkerState.id], set_={"heartbeat": now()}))
            if not run_once():
                time.sleep(1)


if __name__ == "__main__":
    main()
