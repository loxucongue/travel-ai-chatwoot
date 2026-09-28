"""Separate model and delivery pools; a slow customer never stops other timers."""
import logging
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError
from app.automation_models import AutomationSession
from app.config import settings
from app.db import SessionLocal
from app.models import utcnow
from app.reception_v3 import service

logger = logging.getLogger(__name__)


def model_turn(session_id, environment):
    with SessionLocal() as db:
        return service.tick(db, session_id=session_id, advance_clock=False, environment=environment)


def delivery_turn(session_id, environment):
    for attempt in range(3):
        try:
            with SessionLocal() as db:
                row = db.get(AutomationSession, session_id)
                if not row or row.environment != environment:
                    return
                service.advance(row, deliver=environment == 'playground')
                db.commit()
                if environment == 'live':
                    from app.reception_v3.live import deliver_one
                    deliver_one(db, row)
                return
        except StaleDataError:
            if attempt == 2:
                raise


class Scheduler:
    def __init__(self, environment, concurrency=None):
        self.environment = environment
        self.capacity = concurrency or settings.live_reply_concurrency
        self.models = ThreadPoolExecutor(max_workers=self.capacity, thread_name_prefix='v3-model')
        self.delivery = ThreadPoolExecutor(max_workers=max(2, self.capacity), thread_name_prefix='v3-delivery')
        self.model_jobs, self.delivery_jobs = {}, {}

    @staticmethod
    def collect(jobs):
        for key, future in list(jobs.items()):
            if future.done():
                del jobs[key]
                try:
                    future.result()
                except Exception:
                    logger.exception('v3_task_failed session=%s', key)

    def tick(self):
        self.collect(self.model_jobs)
        self.collect(self.delivery_jobs)
        with SessionLocal() as db:
            rows = db.scalars(select(AutomationSession).where(AutomationSession.environment == self.environment,
                AutomationSession.engine_version == 'v3').order_by(AutomationSession.due_at, AutomationSession.id)).all()
            for row in rows:
                if row.controls.get('simulation', {}).get('status') != 'running':
                    continue
                if row.id not in self.delivery_jobs:
                    self.delivery_jobs[row.id] = self.delivery.submit(delivery_turn, row.id, self.environment)
                value = service.state(row)
                if (row.id not in self.model_jobs and len(self.model_jobs) < self.capacity
                    and value.get('pending_event') and not value.get('failed_event')
                    and value.get('delivery_kind') != 'opening'
                    and (not value.get('retry_at') or service.date(value['retry_at']) <= service.date(utcnow()))
                    and (not value.get('model_not_before') or service.date(value['model_not_before']) <= service.date(utcnow()))):
                    self.model_jobs[row.id] = self.models.submit(model_turn, row.id, self.environment)

    def close(self):
        self.models.shutdown(wait=True)
        self.delivery.shutdown(wait=True)
