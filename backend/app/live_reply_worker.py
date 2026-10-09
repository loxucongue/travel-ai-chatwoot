import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.delivery_status import reconcile_live_delivery
from app.live_reply import assert_worker_ready, mirror_event
from app.models import WebhookEvent, WorkerHeartbeat, utcnow
from app.relay import RelayClient, poll_relay_once
from app.reception_v3.worker import Scheduler
logger=logging.getLogger(__name__)

def heartbeat(worker_id):
    with SessionLocal() as db:
        beat = db.get(WorkerHeartbeat, worker_id) or WorkerHeartbeat(worker_id=worker_id)
        beat.last_seen_at = utcnow()
        db.add(beat)
        db.commit()


def maintenance_tick(receipts_checked, deadlines):
    from app.notification_dispatch import process_handoff_overdue, process_notification_delivery
    from app.advisor_assignment import process_assignment
    errors = deadlines.setdefault("errors", {})
    tasks = [("assignments", lambda: process_assignment(SessionLocal)),
             ("handoffs", lambda: process_handoff_overdue(SessionLocal)),
             ("notifications", lambda: process_notification_delivery(SessionLocal))]
    now = time.monotonic()
    if now >= deadlines.get("receipts", 0):
        tasks.append(("receipts", lambda: reconcile_live_delivery(receipts_checked)))
        deadlines["receipts"] = now + 30
    for name, task in tasks:
        try:
            task()
            errors.pop(name, None)
        except Exception as exc:
            errors[name] = type(exc).__name__
            logger.warning("live_maintenance_failed task=%s code=%s", name, type(exc).__name__)
    if not errors:
        heartbeat("live-maintenance-worker")


def acquire_lock():
    path = Path("data/live-reply.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if path.stat().st_size == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if __import__("os").name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("live_reply_worker_already_running") from None
    return stream


def run():
    with SessionLocal() as db:
        assert_worker_ready(db)
    lock = acquire_lock()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    relay_client = RelayClient() if settings.relay_enabled else None
    try:
        logger.info("live_reply_started concurrency=%s", settings.live_reply_concurrency)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="maintenance") as maintenance:
            scheduler = Scheduler('live')
            receipts_checked, deadlines = {}, {}
            pending_maintenance = None
            next_poll, next_maintenance = 0, 0
            while not lock.closed:
                try:
                    if settings.relay_enabled and time.monotonic() >= next_poll:
                        try:
                            poll_relay_once(relay_client)
                            heartbeat("live-relay-worker")
                        except Exception as exc:
                            logger.warning("relay_poll_failed code=%s", type(exc).__name__)
                        next_poll = time.monotonic() + settings.relay_poll_interval_seconds
                    with SessionLocal() as db:
                        events = db.scalars(select(WebhookEvent).where(WebhookEvent.status.in_(["pending", "retry"]))
                                            .order_by(WebhookEvent.id).limit(100)).all()
                        for event in events:
                            try:
                                mirror_event(db, event)
                            except Exception as exc:
                                db.rollback()
                                event.attempts += 1
                                event.status = "dead" if event.attempts >= 5 else "retry"
                                event.error_code = type(exc).__name__
                                db.commit()
                    scheduler.tick()
                    if pending_maintenance is not None and pending_maintenance.done():
                        finished = pending_maintenance
                        pending_maintenance = None
                        finished.result()
                    if pending_maintenance is None and time.monotonic() >= next_maintenance:
                        pending_maintenance = maintenance.submit(maintenance_tick, receipts_checked, deadlines)
                        next_maintenance = time.monotonic() + 1
                    heartbeat("live-reply-worker")
                    time.sleep(0.5)
                except Exception as exc:
                    logger.warning("live_reply_tick_failed code=%s", type(exc).__name__)
                    time.sleep(2)
    finally:
        if relay_client:
            relay_client.close()
        if "scheduler" in locals():
            scheduler.close()
        lock.close()


if __name__ == "__main__":
    run()
