"""Isolated worker for full-journey playground sessions.

This process only selects ``environment=playground`` rows. It never imports a
Chatwoot client or an outbound sender, so it is safe to run beside live reply
workers.
"""
import logging
import time

from app.automation_service import advance_running_playgrounds, rehearse_tick
from app.db import SessionLocal
from app.models import WorkerHeartbeat, utcnow


logger = logging.getLogger("playground_worker")


def tick(db) -> bool:
    """Process one queued model task and advance every runnable playground clock."""
    processed = rehearse_tick(db, environment="playground")
    advanced = advance_running_playgrounds(db)
    return processed or advanced


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("playground_worker_started environment=playground outbound=false")
    next_heartbeat = 0.0
    while True:
        worked = False
        try:
            with SessionLocal() as db:
                if time.monotonic() >= next_heartbeat:
                    beat = db.get(WorkerHeartbeat, "playground-worker") or WorkerHeartbeat(worker_id="playground-worker")
                    beat.last_seen_at = utcnow()
                    db.add(beat)
                    db.commit()
                    next_heartbeat = time.monotonic() + 5
                worked = tick(db)
        except Exception as exc:
            logger.warning("playground_tick_failed code=%s", type(exc).__name__)
            time.sleep(1)
            continue
        time.sleep(0.25 if worked else 0.5)


if __name__ == "__main__":
    run()
