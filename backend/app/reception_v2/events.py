"""Evidence-backed customer events, isolated from V1's inferred profile."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from app.contact_channels import refusal_scope

EVENT_TYPES = {
    'question', 'material_requested', 'considering', 'contact_agreed',
    'contact_refused', 'human_requested', 'route_selected', 'route_comparison', 'profile_updated',
}


def validate_events(raw, context: dict) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValueError('v2_invalid_events')
    if context.get('module') in {'silence_touch', 'wakeup'}:
        if raw:
            raise ValueError('v2_silence_customer_event_forbidden')
        return []
    text = str(context.get('customer_text') or '')
    result = []
    for item in raw:
        if not isinstance(item, dict) or item.get('type') not in EVENT_TYPES:
            raise ValueError('v2_invalid_event_type: allowed=' + ','.join(sorted(EVENT_TYPES)))
        quote = item.get('quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in text:
            raise ValueError('v2_event_evidence_missing')
        event = {'type': item['type'], 'quote': quote,
                 'source_message_id': context.get('source_message_id') or
                     (context.get('source_message_ids') or [None])[-1],
                 'topic': str(item.get('topic') or '')[:100]}
        event['occurred_at'] = context.get('now') or context.get('virtual_now') or datetime.now(timezone.utc).isoformat()
        if item['type'] == 'material_requested':
            kind = item.get('material_kind') or item.get('topic')
            if kind not in {'itinerary', 'full_introduction', 'hotel', 'vehicle', 'altitude', 'other'}:
                raise ValueError('v2_event_material_kind_required')
            event['material_kind'] = kind
        if item['type'] == 'contact_refused':
            scope = refusal_scope(item.get('scope'))
            if scope is None:
                raise ValueError('v2_contact_refusal_scope_required')
            event['scope'] = scope
        if item['type'] == 'contact_agreed':
            try:
                when = datetime.fromisoformat(str(item.get('contact_at') or '').replace('Z', '+00:00'))
            except ValueError as exc:
                raise ValueError('v2_contact_time_invalid') from exc
            if when.tzinfo is None:
                raise ValueError('v2_contact_timezone_required')
            now = datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00'))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            if when <= now:
                raise ValueError('v2_contact_time_not_future')
            event['contact_at'] = when.isoformat()
        result.append(event)
    return result


def merge_events(slots: dict, events: list[dict]) -> dict:
    """Store customer requests only; an answer draft never becomes a receipt."""
    state = deepcopy(slots.get('_v2_state') or {'schema_version': 1, 'events': []})
    state.setdefault('events', [])
    if any(e['type'] in {'question', 'material_requested', 'route_selected', 'profile_updated'} for e in events) and not any(
        e['type'] == 'considering' for e in events
    ):
        state.pop('reevaluate_at', None)
        state.pop('waiting_reason', None)
    for event in events:
        if event not in state['events']:
            state['events'].append(deepcopy(event))
        if event['type'] == 'contact_refused':
            if event['scope'] == 'all':
                state['proactive_opt_out'] = True
            else:
                state['refused_channels'] = sorted(set(state.get('refused_channels', [])) | {event['scope']})
        elif event['type'] == 'contact_agreed':
            state['proactive_opt_out'] = False
            state['contact_at'] = event['contact_at']
            state['contact_evidence'] = event['quote']
        elif event['type'] == 'considering':
            state['waiting_reason'] = 'considering'
            state['reevaluate_at'] = (datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00')) + timedelta(hours=24)).isoformat()
        elif event['type'] == 'question':
            questions = state.setdefault('questions', [])
            identity = (event['source_message_id'], event['quote'])
            if not any((q['source_message_id'], q['quote']) == identity for q in questions):
                questions.append({**event, 'status': 'pending'})
    state['events'] = state['events'][-100:]
    return {**slots, '_v2_state': state}


def answer_receipt(decision: dict, content: str) -> dict | None:
    if decision.get('action') not in {'reply', 'handoff'} or not content.strip():
        return None
    if decision.get('action') == 'handoff' and not decision.get('evidence_refs'):
        return None
    if decision.get('v2_delivery_sections'):
        section = next((item for item in decision['v2_delivery_sections']
                        if item.get('text', '').strip() == content.strip()), None)
        if section is None:
            return None
        return {'route': decision.get('route_variant', ''),
                'fact_ids': list(section.get('evidence_refs') or []),
                'questions': [e for e in decision.get('v2_events', []) if e.get('type') == 'question']
                    if section.get('answers_customer_question') else []}
    bodies = {str(decision.get(key) or '').strip() for key in ('reply', 'reply_body')}
    if content.strip() not in bodies:
        return None
    return {'route': decision.get('route_variant', ''),
            'fact_ids': list(decision.get('evidence_refs') or []),
            'questions': [e for e in decision.get('v2_events', []) if e.get('type') == 'question']}


def rebuild_answers(slots: dict, receipts: list[dict]) -> dict:
    state = deepcopy(slots.get('_v2_state') or {'schema_version': 1, 'events': []})
    provided = {}
    answered = {(q.get('source_message_id'), q.get('quote')) for receipt in receipts
                for q in receipt.get('questions', [])}
    for receipt in receipts:
        route = receipt.get('route')
        provided[route] = sorted(set(provided.get(route, [])) | set(receipt.get('fact_ids', [])))
    state['provided_fact_ids'] = provided
    for question in state.get('questions', []):
        question['status'] = 'answer_provided' if (question.get('source_message_id'), question.get('quote')) in answered else 'pending'
    return {**slots, '_v2_state': state}
