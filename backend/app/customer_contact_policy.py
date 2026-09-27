"""Customer-owned contact constraints survive engine switches."""
from datetime import datetime, timezone
import math
from app.contact_channels import refusal_scope


def contact_constraint(context: dict) -> tuple[str, int]:
    slots = (context.get('journey') or {}).get('slots') or context.get('memory') or {}
    state = slots.get('_v2_state') or {}
    if state.get('proactive_opt_out'):
        return 'customer_opted_out', 0
    due = state.get('contact_at') or state.get('reevaluate_at')
    if not due:
        return '', 0
    now = datetime.fromisoformat(str(context.get('now') or context.get('virtual_now') or datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00'))
    when = datetime.fromisoformat(due.replace('Z', '+00:00'))
    minutes = math.ceil((when - now).total_seconds() / 60)
    return ('customer_requested_time', min(720, max(1, minutes))) if minutes > 0 else ('', 0)


V2_OPT_OUT_RECEIPT = '好的，之後不會再主動聯繫您。有需要時再告訴我就好。'
V2_APPOINTMENT_RECEIPT = '我會把您希望的聯繫時間交給顧問安排，這段時間先不打擾您。'


def current_contact_refusals(context: dict) -> list[dict]:
    """Current validated events only; semantic scope is independently audited."""
    if context.get('module') in {'silence_touch','wakeup'}:
        return []
    current=str(context.get('customer_text') or '')
    return [{'scope':refusal_scope(event.get('scope'))} for event in context.get('v2_events',[])
            if event.get('type')=='contact_refused' and refusal_scope(event.get('scope')) is not None
            and event.get('quote') and event['quote'] in current]
