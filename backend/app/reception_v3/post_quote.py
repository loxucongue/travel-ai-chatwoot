"""One-shot post-quote review on the existing durable silence clock."""
from datetime import datetime, timedelta, timezone

DELIVERED = {'simulated_delivered', 'submitted', 'sent', 'delivered', 'read'}


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def eligible(value, profile, messages):
    anchor = value.get('post_quote_pending', {}).get('quoted_at')
    return not (any(value.get(k) for k in ('human_followup', 'handoff', 'opt_out', 'lead_assist'))
                or anchor and any(m.get('direction') == 'incoming' and m.get('created_at')
                                  and stamp(m['created_at']) > stamp(anchor) for m in messages)
                or profile.get('contact_capture_declined') or profile.get('contact_value')
                or profile.get('contact_status') in ('captured', 'self_reported_added', 'provided', 'attachment_pending_verification')
                or any(m.get('contact_invitation') and m.get('direction') == 'outgoing'
                       and m.get('status') in DELIVERED for m in messages))


def arm(value, profile, messages, plan_id, now):
    """Only a completely delivered quote plan creates a timer; never old history."""
    if value.get('post_quote_last_plan') == plan_id:
        if value.get('post_quote_pending', {}).get('plan_id') == plan_id:
            value['next_check_at'] = value['post_quote_pending']['due_at']
        return False
    parts = [m for m in messages if m.get('plan_id') == plan_id]
    quotes = [m for m in parts if m.get('post_quote_minutes')]
    if not quotes and value.get('post_quote_pending') and eligible(value, profile, messages):
        value['next_check_at'] = value['post_quote_pending']['due_at']
        return False
    if (not quotes or not all(m.get('status') in DELIVERED for m in parts)
            or not eligible(value, profile, messages)
            or value.get('post_quote_last_plan') == plan_id):
        return False
    due = (stamp(now) + timedelta(minutes=max(m['post_quote_minutes'] for m in quotes))).isoformat()
    value.update(post_quote_last_plan=plan_id,
                 post_quote_pending={'plan_id': plan_id, 'quoted_at': now, 'due_at': due, 'quote_ids': [m['id'] for m in quotes]},
                 next_check_at=due, next_check_minutes=max(m['post_quote_minutes'] for m in quotes),
                 appointment_pending=True)
    return True


def due(context):
    pending = context.get('state', {}).get('post_quote_pending')
    return bool(context.get('event') == 'silence_due' and pending
                and stamp(context['now']) >= stamp(pending['due_at']))


def consume(value):
    value.pop('post_quote_pending', None)
    value['appointment_pending'] = False
