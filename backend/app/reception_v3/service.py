"""Shared durable V3 reception and delivery state."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm.exc import StaleDataError

from app.automation_models import AutomationRun, AutomationSession
from app.material_library import candidate_materials, resolve_materials, tenant_for_session
from app.models import utcnow
from app.model_metering import calls, metrics
from app.opening_messages import delivery_items, opening_media_info
from app.reception_v3 import release_id
from app.reception_v3.runtime import run_agent
from app.reception_v3.skills import compile_skills
from app.reception_v3.quotes import personalize_quotes, reserve_quoted_offers
from app.reception_v3.service_voice import service_voice
from app.reception_v3.seasonal_quotes import mentions_departure_time
from app.reception_v3.followup_window import FOLLOWUP_WINDOW, deadline
from app.reception_v3.route_choice import choice_error, needs_route_review, rejects_current_route
from app.web_knowledge import active_web_facts


def date(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def later(value, seconds):
    return (date(value) + timedelta(seconds=seconds)).isoformat()


def state(row):
    return deepcopy((row.controls or {}).get('v3', {}))


def save(row, value):
    row.controls = {**row.controls, 'v3': value}


def followup_response(messages):
    """Attribute only the first reply after an actually submitted proactive touch.

    A newer inbound or ordinary advisor/AI answer ends the attribution window.
    Draft/cancelled/failed messages never establish a touch.
    """
    for message in reversed(messages):
        if message.get('direction') == 'incoming':
            return None
        if (message.get('direction') == 'outgoing' and message.get('status') in
                ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')):
            return ({'message_id': message.get('id'), 'plan_id': message.get('plan_id'),
                     'sent_at': message.get('sent_at') or message.get('created_at')}
                    if message.get('proactive') else None)
    return None


def followup_materials(materials, messages, route, profile=None):
    """Offer new route photos, including deduplication across catalog aliases."""
    if (rejects_current_route(route, profile or {})
            or (profile or {}).get('discussion_route') not in (None, '', route)):
        return []
    by_key = {m['key']: m for m in materials}
    sent_keys, sent_hashes, sent_ids = set(), set(), set()
    for message in messages:
        if (message.get('direction') != 'outgoing' or message.get('status') not in
                ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')):
            continue
        key = message.get('asset_key')
        if key:
            sent_keys.add(key)
        digest = message.get('media_hash') or by_key.get(key, {}).get('media_hash')
        if digest:
            sent_hashes.add(digest)
        if message.get('media_id'):
            sent_ids.add(message['media_id'])
    result, offered = [], set()
    # Prefer catalog entries with a useful caption over bare import aliases.
    for material in sorted(materials, key=lambda m: bool(m.get('what_it_shows')), reverse=True):
        identity = material.get('media_hash') or material.get('media_id') or material['key']
        if (route not in material.get('routes', []) or material.get('content_type') != 'image'
                or material['key'] in sent_keys or material.get('media_hash') in sent_hashes
                or material.get('media_id') in sent_ids or identity in offered):
            continue
        offered.add(identity)
        result.append(material)
    return result


def opening_parts(db, row, bundle, *, text_only=False):
    reply = bundle['reply']
    texts = reply.get('opening_messages') or [reply.get('opening_message', '')]
    parts = []
    for item in delivery_items(reply.get('opening_items'), [t for t in texts if t]):
        if text_only and item['content_type'] != 'text':
            continue
        info = opening_media_info(db, item, tenant_for_session(db, row)) if item['content_type'] != 'text' else {}
        parts.append({**info, 'content': item.get('content', ''), 'content_type': item['content_type'],
                      'interval_seconds_after': reply.get('opening_interval_seconds', 2)})
    return parts


def begin_intake(row, value, bundle, route):
    intake = {**bundle.get('intake', {}), **(bundle['routes'].get(route, {}).get('intake') or {})}
    field = intake.get('field', 'party_size')
    known = (row.memory.get('departure_window') or row.memory.get('departure_date_start')) if field == 'departure_window' else (row.memory.get('party_size') or row.memory.get('party_size_range'))
    if (not intake.get('enabled') or route not in bundle['routes'] or value.get('handoff') or value.get('opt_out') or value.get('intake_status') == 'done'
            or value.get('introduction_route') or value.get('completed_introductions')
            or known):
        return False
    row.controls = {**row.controls, 'route_variant': route}
    if value.get('intake_status') in ('sending', 'waiting'):
        return True
    value.update(intake_status='sending', intake_field=field, intake_wait_seconds=intake['wait_seconds'],
                 pending_event=None, intake_deadline=None)
    append_plan(row, value, [{'content': intake['question'], 'content_type': 'input_select',
        'content_attributes': {'items': [{'title': option, 'value': option} for option in intake['options']]}}],
        'intake', 1)
    return True


def followup_intervals(value, silence):
    if silence.get('daily_at'):
        return [1440]
    initial = silence.get('initial_checkpoints_minutes')
    if initial is None:
        return silence.get('intervals_minutes', [])
    points = [] if value.get('followup_customer_replied') else list(initial)
    interval = silence.get('recurring_interval_minutes', 180)
    next_point = (points[-1] if points else 0) + interval
    while next_point <= FOLLOWUP_WINDOW.total_seconds() / 60:
        points.append(next_point)
        next_point += interval
    return [point - (points[i-1] if i else 0) for i, point in enumerate(points)]


def active_followup_time(moment, silence):
    clock = date(moment).astimezone(ZoneInfo('Asia/Taipei')).strftime('%H:%M')
    start, end = silence.get('active_start', '00:00'), silence.get('active_end', '23:59')
    return start <= clock < end if start < end else clock >= start or clock < end


def schedule_followup(value, bundle, now, *, reset=False):
    silence = bundle['silence']
    if silence.get('daily_at'):
        schedule_daily_followup(value, silence, now, reset=reset)
        return
    initial = silence.get('initial_checkpoints_minutes')
    intervals = followup_intervals(value, silence)
    if reset or not value.get('followup_anchor'):
        value['followup_anchor'] = (value.get('followup_entry_at', now)
            if initial is not None and not value.get('followup_customer_replied') else now)
    anchor = value['followup_anchor']
    customer = value.get('followup_last_customer_at') or value.get('followup_entry_at') or anchor
    value['followup_until'] = deadline(customer).isoformat()
    value['next_check_at'] = None
    for index in range(value.get('silence_step', 0), len(intervals)):
        target = later(anchor, sum(intervals[:index+1]) * 60)
        first_hour = initial is not None and not value.get('followup_customer_replied') and sum(intervals[:index+1]) <= 60
        if initial is not None and not first_hour and not active_followup_time(target, silence):
            continue  # Skip quiet-hour slots; do not batch them at opening time.
        if date(target) > date(now) and date(target) <= date(value['followup_until']):
            value.update(silence_step=index, next_check_at=target)
            break


def schedule_daily_followup(value, silence, now, *, reset=False):
    """Next local calendar day, at most once; never extend the customer window."""
    if reset or not value.get('followup_anchor'):
        value['followup_anchor'] = now
    zone = ZoneInfo('Asia/Taipei')
    anchor = date(value['followup_anchor']).astimezone(zone)
    current = date(now).astimezone(zone)
    customer = date(value.get('followup_last_customer_at') or value.get('followup_entry_at') or value['followup_anchor'])
    value['followup_until'] = deadline(customer.isoformat()).isoformat()
    hour, minute = map(int, silence['daily_at'].split(':'))
    target = (anchor + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Missed dates are skipped, not replayed in a burst. A previously delivered
    # photo and caption count as one touch even if they cross midnight.
    last_touch = value.get('daily_last_touch_at')
    earliest = max(anchor.date() + timedelta(days=1), current.date())
    if last_touch:
        earliest = max(earliest, date(last_touch).astimezone(zone).date() + timedelta(days=1))
    target = target.replace(year=earliest.year, month=earliest.month, day=earliest.day)
    if target <= current:
        target += timedelta(days=1)
    value['next_check_at'] = target.isoformat() if target < date(value['followup_until']) else None
    if not value['next_check_at']:
        value['followup_skip_reason'] = 'outside_channel_window'
    else:
        value.pop('followup_skip_reason', None)


def start(db, row, entry, duration_minutes=525600):
    row.engine_release_id = release_id()
    row.controls = {**row.controls, 'simulation': {
        'status': 'running', 'start_virtual_at': row.virtual_now,
        'end_virtual_at': later(row.virtual_now, duration_minutes * 60),
        'last_wall_at': utcnow(), 'duration_minutes': duration_minutes, 'speed_multiplier': 1,
    }, 'v3': {'buffered_questions': [], 'opt_out': False, 'handoff': False}}
    add_message(db, row, entry, f'entry-{uuid4().hex}')


def add_message(db, row, content, client_key, content_type='text', attachments=None):
    if any(m.get('client_key') == client_key for m in row.messages):
        return
    value = state(row)
    returned = followup_response(row.messages)
    if returned:
        value['followup_return'] = {**returned, 'reply_message_id': client_key,
                                    'replied_at': row.virtual_now}
    elif value.get('followup_return', {}).get('reply_message_id') not in value.get('buffered_questions', []):
        value.pop('followup_return', None)
    previous_incoming = [m for m in row.messages if m.get('direction') == 'incoming']
    value.setdefault('followup_entry_at', previous_incoming[0].get('created_at', row.virtual_now) if previous_incoming else row.virtual_now)
    if previous_incoming:
        value['followup_customer_replied'] = True
    # A reply stops an in-flight silence plan, including its unsent caption.
    proactive_plans = {m.get('plan_id') for m in row.messages if m.get('proactive') and m.get('status') == 'draft'}
    row.messages = [{**m, 'status': 'cancelled'} if m.get('proactive') and m.get('status') == 'draft' else m for m in row.messages]
    if value.get('delivery_plan_id') in proactive_plans:
        value.update(delivery_kind=None, delivery_due_at=None)
    row.generation += 1
    for key in ('failed_event', 'attempts', 'retry_at', 'last_error'):
        value.pop(key, None)
    incoming = {'id': client_key, 'client_key': client_key, 'direction': 'incoming',
                'content': content, 'content_type': content_type, 'created_at': row.virtual_now,
                'timeline_sequence': len(row.messages) + 1}
    if attachments:
        from app.reception_v3.customer_images import attachment_metadata
        incoming['attachments'] = attachment_metadata(attachments)
    row.messages = [*row.messages, incoming]
    # Clicking a second configured product card during intake is comparison,
    # not an answer to the intake question or permission to replay two tours.
    if (value.get('intake_status') in ('sending', 'waiting')
            and not value.get('introduction_route') and not value.get('completed_introductions')
            and not value.get('handoff') and not value.get('opt_out') and not row.memory):
        from app.reception_v3.route_choice import entry_comparison
        comparison = entry_comparison(row.messages, compile_skills(db)['routes'])
        if comparison:
            row.controls = {**row.controls, 'route_variant': comparison['preferred_route']}
            value['entry_comparison'] = comparison
            value['followup_last_customer_at'] = row.virtual_now
            value['buffered_questions'] = [*value.get('buffered_questions', []), client_key]
            value['pending_event'] = None
            save(row, value)
            row.due_at = None
            return
    if value.get('delivery_kind') == 'introduction' and mentions_departure_time(content):
        # The customer may answer after the 30-second intake timer fired. Stop
        # delivery immediately, before a model/network round trip can leak old media.
        value['departure_review_pending'] = True
    if value.get('delivery_kind') == 'introduction' and needs_route_review(content):
        # Pause only; the Agent must distinguish concern, comparison and a real
        # selection before allowing the next image to leave the queue.
        value['route_review_pending'] = True
    value['buffered_questions'] = [*value.get('buffered_questions', []), client_key]
    value['next_check_at'] = None
    value['next_check_minutes'] = None
    value['silence_step'] = 0
    value['followup_last_customer_at'] = row.virtual_now
    for key in ('followup_anchor', 'followup_until', 'appointment_pending'):
        value.pop(key, None)
    if value.get('intake_status') in ('sending', 'waiting'):
        value.update(intake_status='done', intake_deadline=None, intake_answered=True)
        row.messages = [{**m, 'status': 'cancelled'} if m.get('v3_kind') == 'intake' and m.get('status') == 'draft' else m for m in row.messages]
        if value.get('delivery_kind') == 'intake':
            value.update(delivery_kind=None, delivery_due_at=None)
    value['pending_event'] = 'customer_message'
    # Handoff remains terminal for automatic reception. A new user question
    # after opt-out is different: it may be answered without restoring outreach.
    if value.get('handoff'):
        value['pending_event'] = None
    if not value.get('opening_started'):
        bundle = compile_skills(db)
        if bundle.get('intake', {}).get('enabled'):
            # Exact configured advertisement titles only; free text is understood by the Agent.
            from app.reception_v3.route_choice import advertisement_route
            route = advertisement_route(content, bundle['routes'])
            begin_intake(row, value, bundle, route)
        else:
            value['opening_started'] = True
            append_plan(row, value, opening_parts(db, row, bundle), 'opening', bundle['reply'].get('opening_interval_seconds', 2))
    save(row, value)
    row.due_at = utcnow() if value.get('pending_event') else None


def append_plan(row, value, parts, kind, interval, run_id=None):
    if not parts:
        return
    plan_id = uuid4().hex
    messages = list(row.messages)
    for index, part in enumerate(parts):
        if part.get('content_type', 'text') == 'text' and part.get('content'):
            part = {**part, 'content': service_voice(part['content'])}
        messages.append({**part, 'id': f'v3-{uuid4().hex}', 'direction': 'outgoing',
                         'status': 'draft', 'created_at': row.virtual_now,
                         'timeline_sequence': len(messages) + 1, 'run_id': run_id,
                         'v3_kind': kind, 'plan_id': plan_id, 'preserve_delivery_timing': True})
    row.messages = messages
    value['delivery_kind'] = kind
    value['delivery_plan_id'] = plan_id
    value['delivery_interval'] = interval
    value['delivery_due_at'] = row.virtual_now


def scripts(bundle, route):
    result = {s['id']: {'text': s['text'], 'assets': []}
              for s in bundle['common_scripts'] if s.get('enabled', True)}
    result.update({s['id']: {'text': s['answer_text'], 'assets': s.get('asset_ids', [])}
                   for s in bundle['routes'].get(route, {}).get('scripts', []) if s.get('status') == 'active'})
    for key, group in bundle['routes'].get(route, {}).get('groups', {}).items():
        result.setdefault(key, {'text': group['text'], 'assets': group['assets']})
    return result


def parts_for(db, row, bundle, decision, *, followup=False):
    from app.reception_v3.seasonal_quotes import seasonal_group
    from app.reception_v3.party_quotes import quote_error as party_quote_error
    route = decision['route_variant'] or row.controls.get('route_variant', '')
    if route and route not in bundle['routes']:
        raise ValueError('v3_unknown_route')
    if party_quote_error(decision, bundle, row.memory or {}, route):
        raise ValueError('party_quote_not_validated')
    parts = []
    tenant_id = tenant_for_session(db, row)
    materials = {m['key']: m for m in candidate_materials(db, tenant_id)}
    from app.reception_v3.material_rules import eligible_rules, selected_asset, conversation_labels, rule_digest
    rules = {r['id']: r for r in eligible_rules(bundle, list(materials.values()), route,
        conversation_labels(db, row), 'silence_due' if followup else 'customer_message', row.messages)}
    decision = deepcopy(decision)
    configured_only = followup and bundle.get('silence', {}).get('material_selection') == 'configured_rules'
    for message in decision['messages']:
        rule_id = message.get('material_rule_id')
        if not rule_id:
            if configured_only and message['asset_keys']:
                raise ValueError('followup_requires_material_rule')
            continue
        if rule_id not in rules or message['asset_keys']:
            raise ValueError('material_rule_not_eligible')
        rule = rules[rule_id]
        message['asset_keys'] = selected_asset(rule, row.id, row.generation)
        message['text'] = message['text'] or rule['approved_text']
    if followup:
        # Route comparison is valid during a customer answer, not unsolicited outreach.
        allowed = {m['key'] for m in followup_materials(list(materials.values()), row.messages, route, row.memory)}
        keys = [key for message in decision['messages'] for key in message['asset_keys']]
        if (route != row.controls.get('route_variant', '') or decision['start_introduction']
                or len(keys) > 1 or any(key not in allowed for key in keys)):
            raise ValueError('invalid_silence_followup_materials')
    groups = bundle['routes'].get(route, {}).get('groups', {})
    effective_groups = {key: seasonal_group(group, row.memory or {}) for key, group in groups.items()}
    blocked_assets = {asset for key, group in groups.items() for asset in group.get('assets', [])
                      if asset not in effective_groups[key].get('assets', [])}
    delivered = [m for m in row.messages if m.get('direction') == 'outgoing'
                 and m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')]
    seen_keys = {m.get('asset_key') for m in delivered} - {None}
    seen_hashes = {m.get('media_hash') or materials.get(m.get('asset_key'), {}).get('media_hash')
                   for m in delivered} - {None}
    seen_ids = {m.get('media_id') for m in delivered} - {None}

    def add(text, keys, mode='text_then_assets', deduplicate=False):
        media = []
        for key in keys:
            if key in blocked_assets:
                continue
            # Comparing routes may reference a different route's image without
            # changing the customer's selected route.
            asset_routes = [r for r in materials.get(key, {}).get('routes', []) if r in bundle['routes']]
            asset_route = route if route in asset_routes or not asset_routes else asset_routes[0]
            resolved = resolve_materials(db, [key], asset_route, tenant_id)
            for item in resolved:
                if deduplicate and (key in seen_keys or item.get('media_hash') in seen_hashes
                                    or item.get('media_id') in seen_ids):
                    continue
                media.append(item)
                seen_keys.add(key)
                if item.get('media_hash'):
                    seen_hashes.add(item['media_hash'])
                if item.get('media_id'):
                    seen_ids.add(item['media_id'])
        media_parts = [{**m, 'content': ''} for m in media]
        text_parts = [{'content': text, 'content_type': 'text'}] if text else []
        parts.extend(media_parts + text_parts if mode == 'assets_then_text' else text_parts + media_parts)
    available = scripts(bundle, route)
    for key, group in effective_groups.items():
        if groups[key].get('seasonal_override'):
            available[key] = {'text': group['text'], 'assets': group['assets']}
    for message in decision['messages']:
        script = available.get(message['script_id']) if message['script_id'] else None
        if message['script_id'] and not message['text'] and script is None:
            raise ValueError('v3_unknown_script')
        offset = len(parts)
        add(message['text'] or (script['text'] if script else ''),
            message['asset_keys'])
        if message.get('material_rule_id'):
            original = next(r for r in bundle['material_rules'] if r['id'] == message['material_rule_id'])
            for part in parts[offset:]:
                part.update(material_rule_id=original['id'], material_rule_digest=rule_digest(original))
    if decision['start_introduction']:
        if not route:
            raise ValueError('v3_introduction_requires_route')
        spec = bundle['routes'][route]
        from app.reception_v3.material_rules import itinerary_previews
        preview = itinerary_previews(bundle, list(materials.values()), row.messages).get(route, {})
        itinerary_keys = {m['key'] for m in preview.get('assets', [])}
        comparison = state(row).get('entry_comparison') or {}
        if (comparison.get('preferred_route') == route
                and not any(m.get('entry_comparison_summary') for m in delivered)):
            add(comparison['summary'], [])
            parts[-1]['entry_comparison_summary'] = True
        previous_route = row.controls.get('route_variant', '')
        switching = bool(previous_route and previous_route != route and (
            state(row).get('introduction_route') or state(row).get('completed_introductions')
            or any(m.get('v3_kind') == 'introduction' for m in delivered)))
        sequence = spec.get('switch_introduction_sequence') if switching else None
        for key in sequence or spec['introduction_sequence']:
            group = seasonal_group(spec['groups'][key], row.memory or {})
            add(group['text'], group['assets'], group.get('delivery_mode', 'assets_then_text'),
                deduplicate=switching or bool(itinerary_keys.intersection(group['assets'])))
    return route, parts


def update_profile(db, row, run, decision):
    previous_memory = row.memory
    profile = {k:v for k,v in decision['profile'].items() if k != '_route_offer'}
    if (decision.get('route_choice','unchanged')!='recommend' and profile.get('discussion_route')
            and profile['discussion_route']!=(row.memory or {}).get('discussion_route')):
        profile.pop('discussion_route')
    memory = {**(row.memory or {}), **profile}
    latest = next((m for m in reversed(row.messages) if m.get('direction') == 'incoming'), {})
    choice = decision.get('route_choice', 'unchanged')
    offer = (row.memory or {}).get('_route_offer') or {}
    if choice == 'recommend':
        if '_offered_departure_check' not in profile:
            memory.pop('_offered_departure_check', None)
        memory['_route_offer'] = {'route':profile['discussion_route'], 'status':'offered',
            'source_message_id':latest.get('id'), 'updated_at':row.virtual_now}
    elif (choice == 'selected' and decision.get('route_variant')
          and not (offer.get('status') == 'offered' and offer.get('route') != decision['route_variant']
                   and rejects_current_route(offer['route'],memory))):
        memory['_route_offer'] = {**offer, 'route':decision['route_variant'], 'status':'accepted',
            'source_message_id':latest.get('id'), 'updated_at':row.virtual_now}
    elif offer and (rejects_current_route(offer['route'], memory)
                   or 'discussion_route' in profile and not profile['discussion_route']):
        memory['_route_offer'] = {**offer, 'status':'rejected',
            'source_message_id':latest.get('id'), 'updated_at':row.virtual_now}
        memory.pop('discussion_route', None)
        memory.pop('_offered_departure_check', None)
    if profile.get('party_size') is not None:
        memory.pop('party_size_range', None)
        memory.pop('party_size_min', None)
    elif profile.get('party_size_range'):
        memory.pop('party_size', None)
        if 'party_size_min' not in profile:
            memory.pop('party_size_min', None)
    row.memory = memory
    if row.environment == 'live' and any(
            row.memory.get(k) != (previous_memory or {}).get(k) for k in ('party_size', 'party_size_range', 'party_size_min', 'trip_days')):
        from app.advisor_assignment import emit
        from app.models import ConversationState
        conversation = db.get(ConversationState, row.conversation_state_id)
        assignment = emit(db, conversation, ['customer.profile'], f'profile:{run.id}',
                          {**row.memory, 'route_variant': decision['route_variant'] or row.controls.get('route_variant',''),
                           'summary': decision.get('handoff_reason') or '客户确认了人数或行程天数'})
        if assignment and assignment.rule_id and assignment.action == 'handoff':
            run.trace = {**(run.trace or {}), 'assignment_rule': assignment.rule_id}
            return True
    return False


def apply_decision(db, row, run, bundle, decision):
    value = state(row)
    problem = choice_error(decision, {'skills':bundle, 'route_variant':row.controls.get('route_variant',''),
        'profile':row.memory or {}, 'state':value, 'event':run.input_snapshot.get('event', 'customer_message'),
        'messages':row.messages,
        'available_materials':candidate_materials(db, tenant_for_session(db, row))})
    if problem:
        raise ValueError('route_choice_not_validated:' + problem)
    value.pop('departure_review_pending', None)
    value.pop('route_review_pending', None)
    if value.get('human_followup') and decision['action'] != 'handoff':
        # Follow-up permission cannot reopen intake, reprice, or change profile.
        if decision['action'] not in ('reply','wait') or decision['start_introduction'] or len(decision['messages'])>1:
            raise ValueError('invalid_advisor_followup')
        parts=[{'content':m['text'],'content_type':'text'} for m in decision['messages'] if m['text']]
        if any(m['script_id'] or m['asset_keys'] for m in decision['messages']):
            raise ValueError('invalid_advisor_followup')
        value.update(pending_event=None,next_check_at=None,next_check_minutes=1440,appointment_pending=False,
                     last_reason=decision['reason'],followup_settings=deepcopy(bundle['silence']))
        append_plan(row,value,parts,'reply',2,run.id)
        row.messages=[{**m,'proactive':True} if m.get('run_id')==run.id else m for m in row.messages]
        if not parts:
            schedule_followup(value,bundle,row.virtual_now,reset=True)
        save(row,value);row.due_at=None
        return
    if (value.get('intake_answered') and not value.get('completed_introductions')
            and (value.get('intake_field') != 'departure_window' or decision['start_introduction'])
            and not value.get('opt_out') and not value.get('handoff')
            and decision['action'] != 'handoff' and decision['opt_out'] is not True
            and not decision['interrupt'] and decision.get('route_choice') != 'recommend'):
        decision = {**decision, 'start_introduction': True,
                    'messages': decision['messages'] if value.get('intake_field') == 'departure_window' else []}
        run.trace = {**(run.trace or {}), 'execution_reason': 'intake_answer_starts_introduction'}
        run.decision = decision
    previous_memory = row.memory
    if update_profile(db, row, run, decision):
        return
    if decision.get('route_choice') == 'selected' and decision.get('route_variant'):
        # Acceptance closes the offer; it does not erase any customer slots or
        # alter the human/AI owner controls.
        row.memory = {k:v for k,v in row.memory.items() if k not in ('discussion_route','_offered_departure_check')}
        if (row.memory.get('_departure_check') or {}).get('route') not in (None, decision['route_variant']):
            row.memory = {k:v for k,v in row.memory.items() if k != '_departure_check'}
    route_policy = bundle['routes'].get(decision['route_variant'] or row.controls.get('route_variant', ''), {})
    party = row.memory.get('party_size') or row.memory.get('party_size_min')
    if route_policy.get('seasonal_pricing') and isinstance(party, (int, float)) and party >= 7:
        decision = {**decision, 'action':'handoff', 'messages':[], 'start_introduction':False,
                    'handoff_type':'party_size', 'handoff_reason':'常規線7人以上需顧問安排專屬團型。'}
        run.decision = decision
    if decision['action'] == 'handoff':
        # Handoff is an internal action, including this turn. Do not resolve
        # or enqueue the model's text/assets, even if it supplied a farewell.
        value.update(handoff=True, handoff_reason=decision['handoff_reason'],
                     handoff_type=decision.get('handoff_type', 'other'),
                     next_check_minutes=None, buffered_questions=[], last_reason=decision['reason'])
        row.controls = {**row.controls, 'human': True}
        if decision['route_variant']:
            row.controls = {**row.controls, 'route_variant': decision['route_variant']}
        save(row, value)
        control(row, 'stop')
        run.trace = {**(run.trace or {}), 'execution_reason': 'silent_handoff',
                     'suppressed_handoff_messages': len(decision['messages'])}
        if row.environment == 'live':
            from app.reception_v3.live import handoff_from_decision
            handoff_from_decision(db, row)
        return
    selected_route = decision['route_variant'] or row.controls.get('route_variant', '')
    if (selected_route and decision['action'] != 'handoff' and decision['opt_out'] is not True
            and not decision['interrupt'] and row.memory.get('reception_status') != 'abusive'
            and decision.get('route_choice') != 'recommend'
            and not rejects_current_route(selected_route, row.memory)
            and (decision['start_introduction'] or (not decision['messages'] and decision['action'] == 'wait'))):
        if begin_intake(row, value, bundle, decision['route_variant'] or row.controls.get('route_variant', '')):
            save(row, value)
            row.due_at = None
            return
    # Resolve references before changing state, so a missing image cannot leave
    # a half-installed plan or be recorded as delivered.
    try:
        route, parts = parts_for(db, row, bundle, decision,
                                followup=run.input_snapshot.get('event') == 'silence_due')
    except Exception:
        row.memory = previous_memory
        raise
    if decision['opt_out'] is True:
        value['opt_out'] = True
    if decision['interrupt'] or decision['action'] == 'handoff' or decision['opt_out'] is True:
        row.messages = [{**m, 'status': 'cancelled'} if m.get('status') == 'draft' else m for m in row.messages]
        value['delivery_kind'] = None
        value['delivery_due_at'] = None
        value['intake_deadline'] = None
    if value.get('delivery_kind') == 'introduction' and not decision['interrupt'] and decision['action'] != 'handoff':
        value['pending_event'] = None
        row.due_at = None
        save(row, value)
        return
    if route:
        row.controls = {**row.controls, 'route_variant': route}
    if (not route and not value.get('opening_started') and bundle.get('intake', {}).get('enabled')
            and row.memory.get('reception_status') != 'abusive'):
        parts = opening_parts(db, row, bundle, text_only=True) + parts
        value.update(opening_started=True, opening_media_pending=True)
    value['pending_event'] = None
    intake_answered = value.pop('intake_answered', False)
    if not (decision['start_introduction'] and intake_answered):
        value['buffered_questions'] = []
    value['next_check_at'] = None
    step = value.get('silence_step', 0) + (1 if run.input_snapshot.get('event') == 'silence_due' else 0)
    value['silence_step'] = step
    intervals = followup_intervals(value, bundle['silence'])
    configured_delay = 1440 if bundle['silence'].get('daily_at') else (intervals[step] if step < len(intervals) else None)
    stopped = (value.get('opt_out') or value.get('handoff') or not bundle['silence']['enabled']
               or row.memory.get('reception_status') == 'abusive')
    appointment = decision['next_check_minutes'] if run.input_snapshot.get('event') == 'customer_message' else None
    value['appointment_pending'] = bool(appointment)
    value['next_check_minutes'] = None if stopped else (appointment or configured_delay)
    value['last_reason'] = decision['reason']
    kind = 'introduction' if decision['start_introduction'] else 'reply'
    if decision['start_introduction']:
        value['introduction_route'] = route
        value.update(intake_status='done', intake_deadline=None)
        if not value.get('opening_started'):
            parts = opening_parts(db, row, bundle) + parts
            value['opening_started'] = True
        elif value.pop('opening_media_pending', False):
            parts = [p for p in opening_parts(db, row, bundle) if p['content_type'] != 'text'] + parts
    interval = bundle['routes'].get(route, {}).get('interval_seconds', bundle['reply']['opening_interval_seconds'])
    reserve_quoted_offers(db, row, bundle, parts)
    append_plan(row, value, parts, kind, interval, run.id)
    if run.input_snapshot.get('event') == 'silence_due':
        initial = bundle['silence'].get('initial_checkpoints_minutes')
        first_hour = initial is not None and not value.get('followup_customer_replied') and step <= len(initial)
        row.messages = [{**m, 'proactive': True, 'first_hour_followup': first_hour} if m.get('run_id') == run.id else m for m in row.messages]
    if not parts and value['next_check_minutes']:
        if appointment:
            value['next_check_at'] = later(row.virtual_now, appointment * 60)
        else:
            schedule_followup(value, bundle, row.virtual_now, reset=run.input_snapshot.get('event') != 'silence_due')
    value['followup_intervals'] = intervals
    value['followup_settings'] = deepcopy(bundle['silence'])
    save(row, value)
    row.due_at = None


def complete_delivery(row, target, status):
    value = state(row)
    part = next(m for m in row.messages if m.get('id') == target)
    if part.get('proactive') and status in ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read'):
        value['daily_last_touch_at'] = row.virtual_now
    row.messages = [{**m, 'status': status, 'created_at': row.virtual_now,
                     'confirmed_at': row.virtual_now} if m.get('id') == target else m for m in row.messages]
    if part.get('plan_id') != value.get('delivery_plan_id'):
        return
    if any(m.get('status') == 'draft' for m in row.messages):
        value['delivery_due_at'] = later(row.virtual_now, part.get('interval_seconds_after', value['delivery_interval']))
    else:
        kind = value.pop('delivery_kind', None)
        value['delivery_due_at'] = None
        if kind == 'introduction':
            value['completed_introductions'] = list(dict.fromkeys([
                *value.get('completed_introductions', []), value.get('introduction_route')]))
            value['pending_event'] = 'introduction_completed'
            row.due_at = utcnow()
        elif kind == 'intake' and value.get('intake_status') == 'sending':
            value.update(intake_status='waiting', intake_deadline=later(row.virtual_now, value['intake_wait_seconds']))
        elif kind != 'opening' and value.get('next_check_minutes'):
            if value.get('appointment_pending'):
                value['next_check_at'] = later(row.virtual_now, value['next_check_minutes'] * 60)
            else:
                schedule_followup(value, {'silence': value.get('followup_settings', {'intervals_minutes': value.get('followup_intervals', [])})},
                                  row.virtual_now, reset=not part.get('proactive'))
    save(row, value)


def skip_quiet_delivery(row, part):
    """Recheck the clock after model/queue latency, before any channel submission."""
    value = state(row)
    silence = value.get('followup_settings', {})
    if (not part.get('proactive') or part.get('first_hour_followup')
            or (silence.get('initial_checkpoints_minutes') is None and not silence.get('daily_at'))
            or active_followup_time(row.virtual_now, silence)):
        return False
    row.messages = [{**m, 'status': 'cancelled'} if m.get('plan_id') == part.get('plan_id') and m.get('status') == 'draft' else m for m in row.messages]
    value.update(delivery_kind=None, delivery_due_at=None, pending_event=None, followup_skip_reason='quiet_hours_before_delivery')
    schedule_followup(value, {'silence': silence}, row.virtual_now)
    save(row, value)
    return True


def advance(row, wall_now=None, *, deliver=True):
    """Advance wall clock; deliver at most one message, never burst after a stall."""
    simulation = deepcopy(row.controls.get('simulation', {}))
    if simulation.get('status') != 'running':
        return False
    now = wall_now or utcnow()
    seconds = max(0, (date(now) - date(simulation.get('last_wall_at', now))).total_seconds())
    row.virtual_now = later(row.virtual_now, seconds)
    simulation['last_wall_at'] = now
    row.controls = {**row.controls, 'simulation': simulation}
    if simulation.get('end_virtual_at') and date(row.virtual_now) >= date(simulation['end_virtual_at']):
        control(row, 'stop')
        return True
    value = state(row)
    drafts = [m for m in row.messages if m.get('status') == 'draft']
    if drafts:
        if value.get('departure_review_pending') or value.get('route_review_pending'):
            save(row, value)
            return True
        if not deliver:
            save(row, value)
            return True
        if date(value['delivery_due_at']) > date(row.virtual_now):
            return True
        if skip_quiet_delivery(row, drafts[0]):
            return True
        target = drafts[0]['id']
        complete_delivery(row, target, 'simulated_delivered')
        value = state(row)
    elif value.get('intake_deadline') and date(value['intake_deadline']) <= date(row.virtual_now):
        value.update(intake_status='done', intake_deadline=None, pending_event='intake_due')
        row.due_at = utcnow()
    elif value.get('next_check_at') and date(value['next_check_at']) <= date(row.virtual_now):
        value['next_check_at'] = None
        if not value.get('opt_out') and not value.get('handoff'):
            value['pending_event'] = 'silence_due'
            row.due_at = utcnow()
    save(row, value)
    return True


def control(row, action):
    simulation = deepcopy(row.controls.get('simulation', {}))
    simulation['status'] = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}[action]
    simulation['last_wall_at'] = utcnow()
    row.controls = {**row.controls, 'simulation': simulation}
    if action == 'stop':
        row.generation += 1
        row.due_at = None
        row.messages = [{**m, 'status': 'cancelled'} if m.get('status') == 'draft' else m for m in row.messages]
        value = state(row)
        value.update(pending_event=None, delivery_kind=None, delivery_due_at=None, next_check_at=None, intake_deadline=None)
        save(row, value)


def advance_next(row):
    value = state(row)
    if value.get('pending_event') or any(m.get('status') == 'draft' for m in row.messages):
        raise ValueError('journey_busy')
    target = value.get('intake_deadline') or value.get('next_check_at')
    if not target:
        raise ValueError('journey_next_touch_missing')
    row.virtual_now = target
    advance(row)


def tick(db, *, session_id=None, wall_now=None, advance_clock=True, environment='playground'):
    query = select(AutomationSession).where(AutomationSession.engine_version == 'v3', AutomationSession.environment == environment)
    if session_id is not None:
        query = query.where(AutomationSession.id == session_id)
    rows = db.scalars(query.order_by(AutomationSession.id)).all()
    if advance_clock:
        for row in rows:
            advance(row, wall_now)
    db.commit()
    for row in rows:
        value = state(row)
        event = value.get('pending_event')
        if not event or row.controls.get('simulation', {}).get('status') != 'running':
            continue
        if environment == 'live':
            from app.models import ConversationState
            from app.reception_v3.live import permitted
            from app.reception_v3.human_followup import activate_due
            if row.controls.get('human_followup',{}).get('enabled'):
                activate_due(db,db.get(ConversationState,row.conversation_state_id),row)
                db.commit()
            purpose = 'followup' if row.controls.get('human_followup',{}).get('enabled') else 'reply'
            if not permitted(db,db.get(ConversationState,row.conversation_state_id),purpose=purpose):
                continue
        if value.get('failed_event') or (value.get('retry_at') and date(value['retry_at']) > date(utcnow())):
            continue
        # Opening is sent once, verbatim, before asking the model to continue.
        if value.get('delivery_kind') == 'opening':
            continue
        active = db.scalar(select(AutomationRun).where(AutomationRun.session_id == row.id, AutomationRun.status == 'processing'))
        if active and active.lease_until and date(active.lease_until) > date(utcnow()):
            continue
        if active:
            active.status, active.error_code = 'cancelled', 'lease_expired'
        bundle = personalize_quotes(db, row, compile_skills(db))
        if event == 'silence_due':
            silence = bundle['silence']
            if not silence.get('daily_at') and not value.get('appointment_pending') and value.get('followup_anchor'):
                customer = value.get('followup_last_customer_at') or value.get('followup_entry_at') or value['followup_anchor']
                value['followup_until'] = deadline(customer).isoformat()
            incoming_times = [date(m['created_at']) for m in row.messages
                              if m.get('direction') == 'incoming' and m.get('created_at')]
            customer_until = max(incoming_times) + FOLLOWUP_WINDOW if incoming_times else None
            if customer_until:
                value['followup_until'] = customer_until.isoformat()
            expired = (value.get('followup_until')
                       and date(row.virtual_now) >= date(value['followup_until']))
            if value.get('opt_out') or value.get('handoff') or expired or not silence['enabled'] or (environment == 'live' and not silence.get('live_enabled')):
                value.update(pending_event=None, next_check_at=None)
                value['followup_skip_reason'] = ('customer_opt_out' if value.get('opt_out') else
                    'human_handoff' if value.get('handoff') else 'outside_channel_window' if expired else
                    'silence_disabled' if not silence['enabled'] else 'live_silence_disabled')
                row.due_at = None
                save(row, value)
                db.commit()
                continue
            local = date(row.virtual_now).astimezone(ZoneInfo('Asia/Taipei'))
            clock = local.strftime('%H:%M')
            start, end = silence.get('active_start', '00:00'), silence.get('active_end', '23:59')
            initial = silence.get('initial_checkpoints_minutes')
            first_hour = (initial is not None and not value.get('followup_customer_replied')
                and value.get('silence_step', 0) < len(initial)
                and value.get('followup_entry_at')
                and date(row.virtual_now) <= date(value['followup_entry_at']) + timedelta(minutes=61)
                and not value.get('appointment_pending'))
            allowed = active_followup_time(row.virtual_now, silence) or first_hour
            today = {m.get('run_id') for m in row.messages if m.get('proactive') and m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')
                     and date(m['created_at']).astimezone(ZoneInfo('Asia/Taipei')).date() == local.date()}
            if len(today) >= silence.get('max_proactive_messages_per_day', 6):
                value.update(pending_event=None, next_check_at=None, followup_skip_reason='daily_limit')
                row.due_at = None
                save(row, value)
                db.commit()
                continue
            if not allowed:
                if initial is not None and not value.get('appointment_pending'):
                    value.update(pending_event=None, silence_step=value.get('silence_step', 0) + 1)
                    schedule_followup(value, bundle, row.virtual_now)
                    value['followup_skip_reason'] = 'quiet_hours_slot_skipped'
                    row.due_at = None
                    save(row, value)
                    db.commit()
                    continue
                opening = local.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0)
                if opening <= local:
                    opening += timedelta(days=1)
                within_window = value.get('followup_until') and opening <= date(value['followup_until'])
                value.update(pending_event=None, next_check_at=opening.isoformat() if within_window else None)
                value['followup_skip_reason'] = 'deferred_to_active_hours' if within_window else 'outside_channel_window'
                row.due_at = None
                save(row, value)
                db.commit()
                continue
        if event == 'silence_due':
            value.pop('followup_skip_reason', None)
        context = {'event': event, 'now': row.virtual_now, 'route_variant': row.controls.get('route_variant', ''),
                   'skills': bundle, 'profile': row.memory, 'state': value,
                   'messages': [m for m in row.messages if m.get('direction') == 'incoming' or m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')],
                   'buffered_questions': [m for m in row.messages if m.get('id') in value.get('buffered_questions', [])],
                   'available_materials': candidate_materials(db, tenant_for_session(db, row))}
        if event == 'silence_due':
            context['followup_materials'] = followup_materials(
                context['available_materials'], row.messages, context['route_variant'], row.memory)
        from app.reception_v3.material_rules import eligible_rules, conversation_labels
        context['material_rules'] = eligible_rules(bundle, context['available_materials'],
            context['route_variant'], conversation_labels(db, row), event, context['messages'])
        if event == 'silence_due' and bundle['silence'].get('material_selection') == 'configured_rules':
            context['followup_materials'] = []
            context['skills'] = deepcopy(bundle)
            for route in context['skills']['routes'].values():
                route['scripts'] = [s for s in route.get('scripts', []) if 'silence_followup' not in s.get('topics', [])]
        intervals = followup_intervals(value, bundle['silence'])
        next_step = value.get('silence_step', 0) + (event == 'silence_due')
        context['followup_schedule'] = {
            'daily_at': bundle['silence'].get('daily_at'),
            'remaining_intervals_minutes': intervals[next_step:],
            'next_configured_minutes': intervals[next_step] if next_step < len(intervals) else None,
            'checkpoints_minutes': [sum(intervals[:i+1]) for i in range(len(intervals))],
            'window_until': value.get('followup_until'),
            'elapsed_since_reply_minutes': round((date(row.virtual_now)-date(value['followup_anchor'])).total_seconds()/60, 1) if value.get('followup_anchor') else None,
            'anchor_kind': ('customer_entry' if bundle['silence'].get('initial_checkpoints_minutes') is not None and not value.get('followup_customer_replied') else 'reply_completed'),
            'elapsed_since_entry_minutes': round((date(row.virtual_now)-date(value['followup_entry_at'])).total_seconds()/60, 1) if value.get('followup_entry_at') else None,
        }
        for direction, label in (('incoming', 'customer'), ('outgoing', 'outgoing')):
            timestamps = [date(m['created_at']) for m in context['messages']
                          if m.get('direction') == direction and m.get('created_at')]
            context['followup_schedule'][f'minutes_since_last_{label}'] = round(
                (date(row.virtual_now)-max(timestamps)).total_seconds()/60, 1) if timestamps else None
        context['website_facts'], context['website_version'] = active_web_facts(
            db, tenant_for_session(db, row), '\n'.join(m.get('content', '') for m in context['messages']
                if m.get('direction') == 'incoming'), environment=environment, candidate_pool=True)
        run = AutomationRun(session_id=row.id, generation=row.generation, module='silence_touch' if event == 'silence_due' else 'reply',
                            idempotency_key=f'v3:{row.id}:{uuid4().hex}', status='processing', input_snapshot=context,
                            lease_until=later(utcnow(), 90))
        # Claim the session generation as well as the event. The API may add a
        # message while this model request is in flight.
        expected = deepcopy(row.controls)
        if event == 'silence_due':
            expected['v3'] = value
        generation = row.generation
        claimed_controls = {**expected, 'v3_lease': uuid4().hex}
        claimed = db.execute(update(AutomationSession).where(AutomationSession.id == row.id,
            AutomationSession.generation == generation, AutomationSession.revision == row.revision)
            .values(due_at=None, controls=claimed_controls, revision=row.revision + 1).execution_options(synchronize_session=False))
        if not claimed.rowcount:
            db.rollback()
            continue
        db.add(run)
        db.commit()
        db.refresh(row)
        run_id = run.id
        http_calls = []
        meter_token = calls.set(http_calls)
        decision, logs = None, []
        try:
            if event == 'intake_due':
                from app.reception_v3.runtime import Decision
                decision = Decision(route_variant=row.controls['route_variant'], start_introduction=True,
                                    reason='开场问题等待到期，开始配置介绍。').model_dump()
                if value.get('handoff') or value.get('opt_out'):
                    decision = Decision(action='wait').model_dump()
                logs, digest = [], bundle['digest']
            else:
                decision, logs, digest = run_agent(context)
            # Reapply the same model result to fresh state after a delivery CAS conflict.
            # Generation and permission checks run on every attempt; no model retry.
            for persistence_attempt in range(3):
                try:
                    db.refresh(row)
                    allowed = True
                    if environment == 'live':
                        # Settings/labels can change while the model is running. A
                        # disabled inbox must not apply a stale profile or handoff.
                        from app.models import Tenant
                        conversation = db.get(ConversationState, row.conversation_state_id)
                        db.refresh(conversation)
                        db.refresh(conversation.inbox)
                        db.refresh(db.get(Tenant, conversation.tenant_id))
                        if conversation.contact:
                            db.refresh(conversation.contact)
                        allowed = permitted(db, conversation, purpose=(
                            'followup' if row.controls.get('human_followup', {}).get('enabled') else 'reply'))
                    run = db.get(AutomationRun, run_id)
                    run.trace = {'engine_version': 'v3', 'event': event, 'skill_digest': bundle['digest'],
                                 'logs': logs, 'request_hash': digest, 'outbound': False,
                                 'loaded_skills': logs[-1].get('loaded_skills', {}) if logs else {},
                                 'skill_tool_calls': [call for log in logs for call in log.get('tool_calls', [])],
                                 **metrics(http_calls)}
                    run.decision = decision
                    if allowed and row.generation == generation and row.controls.get('simulation', {}).get('status') != 'stopped':
                        from app.reception_v3.customer_reactions import remember_reactions
                        row.messages = remember_reactions(row.messages, logs)
                    if row.generation != generation or row.controls.get('simulation', {}).get('status') == 'stopped':
                        run.status, run.error_code = 'cancelled', 'superseded'
                    elif not allowed:
                        run.status, run.error_code = 'cancelled', 'reception_blocked'
                        control(row, 'stop')
                    elif decision['action'] == 'queue' and context['state'].get('delivery_kind') == 'introduction' and state(row).get('delivery_kind') != 'introduction':
                        # Delivery can finish while this classification call is running.
                        # Keep the buffered questions for the completion turn.
                        if not update_profile(db, row, run, decision):
                            value = state(row)
                            value['pending_event'] = 'introduction_completed'
                            save(row, value)
                            row.due_at = utcnow()
                        run.status = 'completed'
                    else:
                        # Account for model time without delivering a draft here. This
                        # also anchors a wait decision to evaluation completion.
                        advance(row, wall_now=wall_now, deliver=False)
                        apply_decision(db, row, run, bundle, decision)
                        value = state(row)
                        for key in ('last_error', 'retry_at', 'attempts', 'failed_event'):
                            value.pop(key, None)
                        save(row, value)
                        run.status = 'completed'
                    run.completed_at, run.lease_until = utcnow(), None
                    db.commit()
                    break
                except StaleDataError:
                    db.rollback()
                    if persistence_attempt == 2:
                        raise
        except Exception as exc:
            db.rollback()
            db.refresh(row)
            run = db.get(AutomationRun, run_id)
            error = str(exc) or type(exc).__name__
            run.status, run.error_code = 'failed', error[:120]
            run.decision = decision or {}
            run.trace = {'engine_version': 'v3', 'event': event, 'outbound': False,
                         'logs': logs or getattr(exc, 'logs', []),
                         'request_hash': getattr(exc, 'digest', None), **metrics(http_calls)}
            blocked = environment == 'live' and not permitted(db,
                db.get(ConversationState, row.conversation_state_id), purpose=(
                    'followup' if row.controls.get('human_followup', {}).get('enabled') else 'reply'))
            if blocked:
                run.status, run.error_code = 'cancelled', 'reception_blocked'
                if row.generation == generation:
                    control(row, 'stop')
            elif row.generation == generation:
                value = state(row)
                value['last_error'] = error[:200]
                value['attempts'] = value.get('attempts', 0) + 1
                if value['attempts'] < 3:
                    value['retry_at'] = later(utcnow(), 2 ** value['attempts'])
                    row.due_at = value['retry_at']
                else:
                    value['failed_event'] = event
                    row.due_at = None
                    save(row, value)
                    if environment == 'live':
                        from app.reception_v3.live import handoff
                        handoff(db, row, 'model_failed', error[:200])
                        value = state(row)
                    else:
                        # Match live terminal behavior without notifying a real
                        # advisor or sending anything from a simulated session.
                        value.update(handoff=True, handoff_type='model_failed',
                                     handoff_reason=error[:200], next_check_at=None,
                                     pending_event=None, delivery_kind=None)
                        row.controls = {**row.controls, 'human': True}
                        save(row, value)
                        control(row, 'stop')
                        value = state(row)
                save(row, value)
        finally:
            calls.reset(meter_token)
        run.completed_at, run.lease_until = utcnow(), None
        db.commit()
        return True
    return False
