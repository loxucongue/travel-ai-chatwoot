"""Pause active, unverifiable live journeys. Dry-run by default; counts only.

Run with workers stopped and the global sending switch disabled before --apply.
This script never reconstructs snapshots or sends messages. A single transaction
owns all changes; review and resume remain explicit operator actions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.automation_models import LiveSopEnrollment, LiveSopJob
from app.db import SessionLocal
from app.live_reply_models import LiveReplyJob
from app.models import ConversationJourney, ConversationState, HandoffTask, utcnow
from app.operations import ensure_handoff
from app.outbound_control import global_message_sending_enabled
from app.route_reply import journey_context_from_values, route_snapshot_from_values


REASON = "delivery_history_review_required"
DETAIL = "Delivery history or route snapshot is unverifiable. Review before resuming automation."


def _unverifiable(journey):
    slots = journey.slots
    if not isinstance(slots, dict):
        return True
    try:
        if route_snapshot_from_values(journey.route_variant, slots) is None:
            return True
        return bool(journey_context_from_values(
            journey.route_variant, journey.stage, slots, journey.sent_groups,
        )["history_unknown"])
    except (TypeError, ValueError, AttributeError, KeyError):
        # Malformed historical JSON is a review case, never a catalog fallback.
        return True


def migrate_delivery_consistency(db, *, apply=False):
    """Return planned counts; caller commits. Active means pending reply or SOP.

    Dry-run never flushes, updates, or creates a handoff. Existing open handoffs
    and their operator notes are preserved. Terminal jobs/history are untouched.
    """
    counts = {"active_journeys": 0, "review_journeys": 0, "reply_jobs": 0,
              "sop_enrollments": 0, "sop_jobs": 0, "handoffs_created": 0,
              "handoffs_reused": 0}
    with db.no_autoflush:
        if apply and global_message_sending_enabled(db):
            raise ValueError("global_message_sending_must_be_disabled")
        journeys = db.scalars(select(ConversationJourney).where(
            ConversationJourney.route_variant != "",
        ).order_by(ConversationJourney.id)).all()
        for journey in journeys:
            replies = db.scalars(select(LiveReplyJob).where(
                LiveReplyJob.conversation_state_id == journey.conversation_state_id,
                LiveReplyJob.status.in_(["queued", "processing"]),
            )).all()
            enrollments = db.scalars(select(LiveSopEnrollment).where(
                LiveSopEnrollment.conversation_state_id == journey.conversation_state_id,
                LiveSopEnrollment.status == "active",
            )).all()
            if not replies and not enrollments:
                continue
            counts["active_journeys"] += 1
            if not _unverifiable(journey):
                continue
            state = db.get(ConversationState, journey.conversation_state_id)
            if state is None:
                raise ValueError("delivery_migration_missing_conversation")
            jobs = db.scalars(select(LiveSopJob).where(
                LiveSopJob.enrollment_id.in_([enrollment.id for enrollment in enrollments]),
                LiveSopJob.status.in_(["scheduled", "waiting_dependency", "processing"]),
            )).all()
            existing = db.scalar(select(HandoffTask).where(
                HandoffTask.conversation_state_id == state.id,
                HandoffTask.status.in_(["pending", "claimed"]),
            ))
            counts["review_journeys"] += 1
            counts["reply_jobs"] += len(replies)
            counts["sop_enrollments"] += len(enrollments)
            counts["sop_jobs"] += len(jobs)
            counts["handoffs_reused" if existing else "handoffs_created"] += 1
            if not apply:
                continue
            now = utcnow()
            for reply in replies:
                reply.status = "blocked"
                reply.error_code = REASON
                reply.completed_at = now
            for enrollment in enrollments:
                enrollment.status = "attention_required"
                enrollment.exit_reason = REASON
                enrollment.completed_at = now
            for job in jobs:
                job.status = "blocked"
                job.reason = REASON
                job.completed_at = now
            ensure_handoff(db, state, REASON, "" if existing else DETAIL)
            if state.effective_ai_state != "HUMAN_HANDOFF":
                state.effective_ai_state = "HUMAN_HANDOFF"
                state.effective_state_reason = f"handoff:{REASON}"
                state.version += 1
                state.updated_at = now
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        with SessionLocal() as db:
            counts = migrate_delivery_consistency(db, apply=args.apply)
            if args.apply:
                # Recheck before commit; deployment must keep workers/switch off.
                db.flush()
                db.expire_all()
                if global_message_sending_enabled(db):
                    raise ValueError("global_message_sending_must_be_disabled")
                db.commit()
        print(json.dumps(counts, sort_keys=True))
        return 0
    except Exception:
        # Database exception strings can contain customer data or parameters.
        print(json.dumps({"errors": 1}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
