from app.lead_capture_models import LeadCaptureState
from sqlalchemy import select
from app.automation_models import AutomationSession
def capture_json(row: LeadCaptureState | None) -> dict:
    return {
        "status": row.status if row else "not_started",
        "request_count": row.request_count if row else 0,
        "requested_at": row.requested_at if row else None,
        "captured_at": row.captured_at if row else None,
        "captured_kinds": row.captured_kinds if row else [],
        "masked_values": row.masked_values if row else {},
        "label_sync_status": row.label_sync_status if row else "not_required",
    }


def journey_context(row):
    return {'route_variant': row.route_variant, 'stage': row.stage, 'slots': row.slots}
