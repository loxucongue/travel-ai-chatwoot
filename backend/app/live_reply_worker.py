"""Single-machine worker for passive replies and explicitly allowlisted SOP tests."""
import logging
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app.config import settings
from app.reception_config import live_silence_enabled
from app.db import SessionLocal
from app.delivery_status import reconcile_live_delivery
from app.live_reply import assert_worker_ready, mirror_event, process_job, recover_jobs
from app.live_reply_models import LiveReplyJob
from app.models import WebhookEvent, WorkerHeartbeat, utcnow
from app.relay import RelayClient, poll_relay_once

logger = logging.getLogger("live_reply")


def heartbeat(worker_id):
    with SessionLocal() as db:
        beat = db.get(WorkerHeartbeat, worker_id) or WorkerHeartbeat(worker_id=worker_id)
        beat.last_seen_at = utcnow()
        db.add(beat)
        db.commit()


class ConversationScheduler:
    """Bounded work, one in-flight task per conversation across replies and SOPs.

    Only the observing thread mutates this map. The process lock remains required;
    SQL claims protect jobs, while this map also protects *different* jobs for one
    conversation. Sessions and network clients are created inside each task.
    """
    def __init__(self, executor, capacity):
        self.executor, self.capacity = executor, capacity
        self.active = {}

    def reap(self):
        for conversation_id, future in list(self.active.items()):
            if future.done():
                del self.active[conversation_id]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("conversation_task_failed code=%s", type(exc).__name__)

    def submit(self, conversation_id, fn, job_id):
        if conversation_id in self.active or len(self.active) >= self.capacity:
            return False
        self.active[conversation_id] = self.executor.submit(fn, job_id)
        return True


def dispatch_due(scheduler):
    from app.automation_models import LiveSopEnrollment, LiveSopJob
    from app.live_sop import process_due_live_sop
    scheduler.reap()
    while len(scheduler.active) < scheduler.capacity:
        excluded = list(scheduler.active)
        with SessionLocal() as db:
            reply = db.scalar(select(LiveReplyJob).where(
                LiveReplyJob.status == "queued", LiveReplyJob.due_at <= utcnow(),
                LiveReplyJob.conversation_state_id.not_in(excluded)
            ).order_by(LiveReplyJob.due_at, LiveReplyJob.id).limit(1))
            sop = None
            if live_silence_enabled(db):
                sop = db.execute(select(LiveSopJob, LiveSopEnrollment.conversation_state_id).join(
                    LiveSopEnrollment, LiveSopEnrollment.id == LiveSopJob.enrollment_id).where(
                    LiveSopJob.status == "scheduled", LiveSopJob.scheduled_at <= utcnow(),
                    LiveSopEnrollment.conversation_state_id.not_in(excluded)
                ).order_by(LiveSopJob.scheduled_at, LiveSopJob.id).limit(1)).first()
            # Both queues compete by due time; continuous replies cannot starve SOPs.
            if sop and (not reply or sop[0].scheduled_at < reply.due_at):
                candidate = (sop[1], process_due_live_sop, sop[0].id)
            elif reply:
                candidate = (reply.conversation_state_id, process_job, reply.id)
            else:
                return
        scheduler.submit(*candidate)


def maintenance_tick(receipts_checked, deadlines):
    from app.notification_dispatch import process_handoff_overdue, process_notification_delivery
    errors = deadlines.setdefault("errors", {})
    tasks = [("handoffs", lambda: process_handoff_overdue(SessionLocal)),
             ("notifications", lambda: process_notification_delivery(SessionLocal))]
    now = time.monotonic()
    if now >= deadlines.get("receipts", 0):
        tasks.append(("receipts", lambda: reconcile_live_delivery(receipts_checked)))
        deadlines["receipts"] = now + 30
    with SessionLocal() as db:
        silence_enabled = live_silence_enabled(db)
    if silence_enabled and now >= deadlines.get("sops", 0):
        from app.live_sop import reconcile_model_route_sops
        tasks.append(("sops", reconcile_model_route_sops))
        deadlines["sops"] = now + 30
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
        with SessionLocal() as db:
            recover_jobs(db)
            if live_silence_enabled(db):
                from app.live_sop import recover_live_sop_jobs
                recover_live_sop_jobs(db)
        logger.info("live_reply_started concurrency=%s", settings.live_reply_concurrency)
        with ThreadPoolExecutor(max_workers=settings.live_reply_concurrency, thread_name_prefix="conversation") as replies, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="maintenance") as maintenance:
            scheduler = ConversationScheduler(replies, settings.live_reply_concurrency)
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
                        from app.automation_shadow import advance_shadow_sops
                        advance_shadow_sops(db)
                    dispatch_due(scheduler)
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
        lock.close()


if __name__ == "__main__":
    run()


