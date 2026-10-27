"""Configured introduction before advisor handoff; no new follow-up owner."""
from copy import deepcopy
from uuid import uuid4

DELIVERED = {'simulated_delivered', 'submitted', 'sent', 'delivered', 'read'}


def count(profile):
    value = profile.get('party_size') or profile.get('party_size_min')
    return value if type(value) is int else None


def policy_for(bundle, route, profile):
    policy = bundle.get('routes', {}).get(route, {}).get('large_party')
    size = count(profile)
    return policy if policy and policy.get('enabled') and size is not None and size >= policy['minimum'] else None


def party_description(profile):
    if type(profile.get('party_size')) is int:
        return str(profile['party_size']) + '位'
    if profile.get('party_size_range'):
        return str(profile['party_size_range']).strip().rstrip('位人 ') + '位'
    return str(count(profile)) + '位以上'


def cancel_pending(row, value):
    pending = value.pop('handoff_after_plan', None)
    if pending:
        row.messages = [{**m, 'status':'cancelled'} if m.get('plan_id') == pending['plan_id']
                        and m.get('status') == 'draft' else m for m in row.messages]
        value.update(delivery_kind=None, delivery_due_at=None)


def undelivered_parts(row, parts):
    from app.reception_v3.service_voice import service_voice
    delivered = [m for m in row.messages if m.get('status') in DELIVERED and m.get('direction') == 'outgoing']
    texts = {service_voice(m['content']).strip() for m in delivered if m.get('content')}
    assets = {m['asset_key'] for m in delivered if m.get('asset_key')}
    hashes = {m['media_hash'] for m in delivered if m.get('media_hash')}
    return [p for p in parts if not (service_voice(p.get('content', '')).strip() in texts
            or p.get('asset_key') in assets or p.get('media_hash') in hashes)]


def validate_policy(policy, groups):
    if not isinstance(policy, dict) or type(policy.get('enabled')) is not bool:
        raise ValueError('large_party_invalid')
    if type(policy.get('minimum')) is not int or policy['minimum'] < 7:
        raise ValueError('large_party_minimum')
    for field in ('introduction_sequence', 'brief_sequence'):
        sequence = policy.get(field)
        if not isinstance(sequence, list) or not sequence or len(sequence) != len(set(sequence)) or any(k not in groups for k in sequence):
            raise ValueError('large_party_sequence')
    for field in ('opening_text', 'completed_text', 'contact_text', 'unknown_route_text'):
        if not isinstance(policy.get(field), str) or not policy[field].strip():
            raise ValueError('large_party_text')
    if 'unknown_preference_text' in policy and (not isinstance(policy['unknown_preference_text'], str)
            or not policy['unknown_preference_text'].strip()):
        raise ValueError('large_party_text')
    overrides = policy.get('group_text_overrides', {})
    if not isinstance(overrides, dict) or any(k not in groups or not isinstance(v, str) for k, v in overrides.items()):
        raise ValueError('large_party_overrides')


def begin(db, row, run, bundle, decision):
    """Return True only when this configured flow installed/kept a delivery plan."""
    from app.reception_v3 import service
    from app.reception_v3.runtime import Decision
    from app.reception_v3.route_choice import rejects_current_route
    value = service.state(row)
    route = decision['route_variant'] or row.controls.get('route_variant', '')
    policy = policy_for(bundle, route, row.memory)
    # Other handoff reasons (contact, explicit human request, missing facts)
    # remain immediate and must never be delayed by a sales introduction.
    eligible = (decision['opt_out'] is not True and not value.get('opt_out')
        and not value.get('handoff') and not row.controls.get('human')
        and not value.get('human_followup') and row.memory.get('reception_status') != 'abusive'
        and (decision['action'] != 'handoff' or decision.get('handoff_type') == 'party_size'))
    if not eligible:
        value.pop('handoff_after_plan', None)
        service.save(row, value)
        return False
    if not route:
        candidate = next((p for key in bundle.get('routes', {})
                          if (p := policy_for(bundle, key, row.memory))), None)
        if candidate:
            value['large_party_intake'] = True
            prompt = candidate.get('unknown_preference_text', candidate['unknown_route_text']) if row.memory.get('trip_days') else candidate['unknown_route_text']
            decision.update(action='reply', start_introduction=False,
                messages=Decision(messages=[{'text':prompt}]).model_dump()['messages'])
            service.save(row, value)
        return False
    if not policy:
        # An exact smaller count supersedes the earlier lower bound.
        previous = value.get('handoff_after_plan')
        cancel_pending(row, value)
        value.pop('large_party_intake', None)
        service.save(row, value)
        if (previous and route in bundle['routes'] and type(row.memory.get('party_size')) is int
                and row.memory['party_size'] < (bundle['routes'][route].get('large_party') or {}).get('minimum', 7)
                and route not in value.get('completed_introductions', [])):
            # Resume the ordinary route after a correction, without replaying
            # the images/text already delivered by either introduction.
            adapted = Decision(route_variant=route, start_introduction=True).model_dump()
            _, parts = service.parts_for(db, row, bundle, adapted)
            parts = undelivered_parts(row, parts)
            value.update(pending_event=None, route_review_pending=False, departure_review_pending=False,
                         introduction_route=route, opening_started=True)
            if parts:
                service.append_plan(row, value, parts, 'introduction', bundle['routes'][route]['interval_seconds'], run.id)
            else:
                value['completed_introductions'] = list(dict.fromkeys([*value.get('completed_introductions', []), route]))
                value['pending_event'] = 'introduction_completed'
            service.save(row, value);row.due_at=None
            return True
        return False
    if decision.get('route_choice') == 'recommend' or rejects_current_route(route, row.memory):
        cancel_pending(row, value)
        service.save(row, value)
        return False
    if row.memory.get('contact_value') or row.memory.get('contact_status') in ('self_reported_added', 'attachment_pending_verification'):
        return False
    # Keep real date/product restrictions. A larger group does not make a
    # closed product available or authorize a different year's timetable.
    from app.reception_v3.seasonal_quotes import departure_quote, month_answer_window, explicit_date_window
    window = row.memory.get('departure_date_start') or row.memory.get('departure_window')
    if window:
        evidence = (month_answer_window(window, row.virtual_now) or explicit_date_window(window)
                    or row.memory.get('_departure_check') or {})
        start = evidence.get('start') or window
        end = evidence.get('end') or row.memory.get('departure_date_end', '')
        check = departure_quote(bundle, route, start, end, row.virtual_now,
            party_size=count(row.memory), group_type='private')
        if check.get('status') == 'unavailable':
            cancel_pending(row, value)
            service.save(row, value)
            return False
    existing = value.get('handoff_after_plan') or {}
    if existing.get('route') == route and existing.get('party_size') == count(row.memory):
        # A new real question gets answered before handoff, then reuses the
        # configured closing. A pure acknowledgement must not replay a plan.
        if any(m.get('status') == 'draft' for m in row.messages) and decision['action'] in ('queue', 'wait'):
            value.update(pending_event=None, route_review_pending=False)
            service.save(row, value);row.due_at=None
            return True
    configured = deepcopy(bundle)
    spec = configured['routes'][route]
    sequence = policy['brief_sequence'] if value.get('large_party_intake') else policy['introduction_sequence']
    spec['introduction_sequence'] = sequence
    spec['switch_introduction_sequence'] = sequence
    for key, text in policy.get('group_text_overrides', {}).items():
        spec['groups'][key]['text'] = text
    complete = route in value.get('completed_introductions', [])
    adapted = Decision(route_variant=route, start_introduction=not complete).model_dump()
    _, parts = service.parts_for(db, row, configured, adapted)
    delivered = [m for m in row.messages if m.get('status') in DELIVERED and m.get('direction') == 'outgoing']
    texts = {m.get('content') for m in delivered if m.get('content')}
    parts = undelivered_parts(row, parts)
    opening = policy['completed_text'] if complete else policy['opening_text']
    opening = opening.replace('{party_size}', str(count(row.memory)))
    opening = opening.replace('{party_description}', party_description(row.memory))
    prefix = [] if opening in texts else [{'content_type':'text', 'content':opening}]
    # Resolve actual questions through the existing validated reply path.
    closing = policy['contact_text']
    answer = []
    if decision.get('answer_before_introduction') and decision['action'] == 'reply' and decision['messages']:
        _, answer = service.parts_for(db, row, bundle, {**decision, 'start_introduction':False})
    parts = answer + prefix + parts + ([] if closing in texts else [{'content_type':'text', 'content':closing}])
    # The model may quote the configured closing. Apply exact, whole-message
    # deduplication within this plan without cutting or rewriting sentences.
    from app.reception_v3.service_voice import service_voice
    unique, identities = [], set()
    for part in parts:
        identity = (part.get('content_type', 'text'), service_voice(part.get('content', '')).strip(),
                    part.get('asset_key'), part.get('media_hash'))
        if identity not in identities:
            unique.append(part)
            identities.add(identity)
    parts = unique
    row.messages = [{**m, 'status':'cancelled'} if m.get('status') == 'draft' else m for m in row.messages]
    row.controls = {**row.controls, 'route_variant':route}
    value.update(pending_event=None, next_check_at=None, next_check_minutes=None, intake_status='done',
                 intake_deadline=None, route_review_pending=False, departure_review_pending=False,
                 delivery_kind=None, delivery_due_at=None, introduction_route=route)
    service.append_plan(row, value, parts, 'large_party', spec['interval_seconds'], run.id)
    if not parts:
        # A distinct empty plan avoids inheriting cancelled old intro parts.
        value['delivery_plan_id'] = uuid4().hex
    value['handoff_after_plan'] = {'plan_id':value.get('delivery_plan_id'), 'route':route,
        'party_size':count(row.memory), 'summary':'高價值・獨立成團；已完成專屬行程介紹，請顧問核對日期、車型、住宿與報價。'}
    if not parts:
        value['pending_event'] = 'handoff_after_delivery'
    service.save(row, value);row.due_at=None
    run.trace = {**(run.trace or {}), 'execution_reason':'large_party_introduction_then_handoff'}
    return True


def finish(db, row):
    from app.reception_v3 import service
    value = service.state(row)
    pending = value.get('handoff_after_plan')
    if (not pending or pending['plan_id'] != value.get('delivery_plan_id')
            or any(m.get('status') in ('draft', 'submitting') for m in row.messages)
            or value.get('opt_out') or value.get('handoff') or row.controls.get('human')
            or value.get('pending_event') != 'handoff_after_delivery'):
        return False
    if count(row.memory) != pending['party_size'] or row.controls.get('route_variant') != pending['route']:
        value.pop('handoff_after_plan', None)
        value['pending_event'] = 'customer_message'
        service.save(row, value)
        return False
    if any(m.get('plan_id') == pending['plan_id'] and m.get('status') not in DELIVERED for m in row.messages):
        return False
    summary = pending['summary'] + ' 人數：' + party_description(row.memory) + '；路線：' + pending['route']
    summary += '；出發：' + str(row.memory.get('departure_date_start') or row.memory.get('departure_window') or '待確認')
    summary += '；天數：' + str(row.memory.get('trip_days') or '待確認')
    latest = next((m.get('content', '') for m in reversed(row.messages) if m.get('direction') == 'incoming'), '')
    summary += '；客戶最新原話：' + latest[:1000]
    value.pop('handoff_after_plan', None)
    value.update(handoff=True, handoff_type='party_size', handoff_reason=summary,
                 completed_introductions=list(dict.fromkeys([*value.get('completed_introductions', []), pending['route']])))
    service.save(row, value)
    if row.environment == 'live':
        from sqlalchemy import select
        from app.models import ConversationLabelAddition, MessageEvent
        label = '高價值・獨立成團'
        key = (row.conversation_state_id, label)
        source = db.scalar(select(MessageEvent.id).where(
            MessageEvent.conversation_state_id == row.conversation_state_id,
            MessageEvent.direction == 'incoming', MessageEvent.private.is_(False)
        ).order_by(MessageEvent.id.desc()).limit(1))
        if source and not db.get(ConversationLabelAddition, key):
            db.add(ConversationLabelAddition(conversation_state_id=row.conversation_state_id,
                label=label, source_message_id=source))
        from app.reception_v3.live import handoff_from_decision
        handoff_from_decision(db, row)
    else:
        row.controls = {**row.controls, 'human':True}
        service.control(row, 'stop')
    return True
