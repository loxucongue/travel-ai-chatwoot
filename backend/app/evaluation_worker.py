import logging
import time
import argparse

from app.config import settings
from app.db import SessionLocal
from app.evaluation_service import process_next_result
from app.evaluation_sync import process_evaluation_sync
from app.automation_service import rehearse_tick
from app.models import WorkerHeartbeat, utcnow


logger = logging.getLogger("evaluation_worker")


def run(observe_webhooks: bool = False, playground_only: bool = False) -> None:
    if settings.outbound_enabled:
        raise RuntimeError("evaluation_worker_requires_outbound_disabled")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("evaluation_worker_started outbound_mode=%s", settings.outbound_mode)
    next_poll=0.0
    next_heartbeat=0.0
    while True:
        if time.monotonic()>=next_heartbeat:
            with SessionLocal() as db:
                row=db.get(WorkerHeartbeat,"evaluation-worker") or WorkerHeartbeat(worker_id="evaluation-worker")
                row.last_seen_at=utcnow()
                db.add(row)
                db.commit()
            next_heartbeat=time.monotonic()+5
        if observe_webhooks and settings.relay_enabled and time.monotonic()>=next_poll:
            from app.relay import poll_relay_once
            try:poll_relay_once()
            except Exception as exc:logger.warning("relay_poll_failed code=%s",type(exc).__name__)
            next_poll=time.monotonic()+settings.relay_poll_interval_seconds
        try:
            with SessionLocal() as db:
                from app.automation_shadow import observe_webhook, advance_shadow_sops
                worked = (observe_webhooks and observe_webhook(db)) or rehearse_tick(db)
                if observe_webhooks and not worked:
                    advance_shadow_sops(db)
                if not worked and not playground_only:
                    worked=process_evaluation_sync(db) or process_next_result(db)
        except Exception as exc:
            logger.warning("evaluation_tick_failed code=%s",type(exc).__name__)
            time.sleep(2)
            continue
        if not worked:
            time.sleep(settings.evaluation_poll_interval_seconds)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--observe-webhooks",action="store_true")
    parser.add_argument("--playground-only",action="store_true")
    args=parser.parse_args()
    run(args.observe_webhooks,args.playground_only)
