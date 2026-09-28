"""V3 model pool and independent sandbox delivery clock."""
import logging
import time
from app.db import SessionLocal
from app.models import WorkerHeartbeat, utcnow
from app.reception_v3.worker import Scheduler

def run():
    logging.basicConfig(level=logging.INFO)
    scheduler=Scheduler('playground')
    try:
        while True:
            scheduler.tick()
            with SessionLocal() as db:
                beat=db.get(WorkerHeartbeat,'playground-worker') or WorkerHeartbeat(worker_id='playground-worker')
                beat.last_seen_at=utcnow()
                db.add(beat)
                db.commit()
            time.sleep(0.5)
    finally:
        scheduler.close()

if __name__ == '__main__':
    run()
