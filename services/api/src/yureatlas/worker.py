import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .execution import JobOwnershipLost, LostOwnership, WorkerOwnershipLost, execution_scope
from .jobs import Cancelled, checkpoint, claim, finish
from .worker_locks import worker_session

log = logging.getLogger("yureatlas.worker")
HEARTBEAT_SECONDS = 15


def dispatch(kind, payload, progress):
    if kind == "ping":
        return {"message": "Worker is ready"}
    from .tasks import execute

    return execute(kind, payload, progress)


def run_once(owner) -> bool:
    job = claim(owner)
    if job is None:
        return False
    stop = threading.Event()
    interruption = []

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                checkpoint(job.id, job.attempt_token, owner)
            except (Cancelled, LostOwnership) as exc:
                interruption.append(exc)
                return
            except Exception:
                # Temporary DB failure does not extend the lease. The next
                # checkpoint or final transaction guard still fails closed.
                log.exception("Heartbeat failed")

    def progress(value):
        if interruption:
            raise interruption[0]
        checkpoint(job.id, job.attempt_token, owner, value)

    def complete(status, *, result=None, error=None):
        try:
            return finish(job.id, job.attempt_token, owner, status, result, error)
        except JobOwnershipLost:
            log.warning("Job %s attempt lost ownership; completion left unchanged", job.id)
            return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(heartbeat)
        try:
            with execution_scope(owner, job_id=job.id, attempt_token=job.attempt_token):
                progress(0)
                result = dispatch(job.kind, job.payload, progress)
            if interruption:
                raise interruption[0]
            complete("completed", result=result)
        except WorkerOwnershipLost:
            raise  # Stop this worker; a successor owns or may acquire the database.
        except JobOwnershipLost:
            log.warning("Job %s attempt lost ownership; later claims may recover it", job.id)
        except Cancelled as exc:
            complete("cancelled", error=str(exc))
        except Exception as exc:
            log.exception("Job %s failed", job.id)
            missed = (
                job.kind == "forecast" and job.payload.get("scheduled") and "deadline" in str(exc).lower()
            )
            complete("missed" if missed else "failed", error=str(exc)[:4000])
        finally:
            stop.set()
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with worker_session() as owner:
        from .scheduling import start_scheduler

        scheduler = start_scheduler(owner=owner)
        try:
            while True:
                if not run_once(owner):
                    time.sleep(1)
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=True)


if __name__ == "__main__":
    main()
