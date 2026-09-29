"""Shared durable V3 reception and delivery state."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.automation_models import AutomationRun, AutomationSession
from app.material_library import candidate_materials, resolve_materials, tenant_for_session
from app.models import utcnow
from app.model_metering import calls, metrics
from app.opening_messages import delivery_items, opening_media_info
from app.reception_v3 import release_id
from app.reception_v3.runtime import run_agent
from app.reception_v3.skills import compile_skills
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


def start(db, row, entry, duration_minutes=525600):
    row.engine_release_id = release_id()
    row.controls = {**row.controls, 'simulation': {
        'status': 'running', 'start_virtual_at': row.virtual_now,
        'end_virtual_at': later(row.virtual_now, duration_minutes * 60),
        'last_wall_at': utcnow(), 'duration_minutes': duration_minutes, 'speed_multiplier': 1,
    }, 'v3': {'buffered_questions': [], 'opt_out': False, 'handoff': False}}
    add_message(db, row, entry, f'entry-{uuid4().hex}')


def add_message(db, row, content, client_key, content_type='text'):
    if any(m.get('client_key') == client_key for m in row.messages):
        return
    value = state(row)
    row.generation += 1
    for key in ('failed_event', 'attempts', 'retry_at', 'last_error'):
        value.pop(key, None)
    incoming = {'id': client_key, 'client_key': client_key, 'direction': 'incoming',
                'content': content, 'content_type': content_type, 'created_at': row.virtual_now,
                'timeline_sequence': len(row.messages) + 1}
    row.messages = [*row.messages, incoming]
    value['buffered_questions'] = [*value.get('buffered_questions', []), client_key]
    value['next_check_at'] = None
    value['next_check_minutes'] = None
    value['silence_step'] = 0
    value['pending_event'] = 'customer_message'
    # Handoff remains terminal for automatic reception. A new user question
    # after opt-out is different: it may be answered without restoring outreach.
    if value.get('handoff'):
        value['pending_event'] = None
    if not value.get('opening_started'):
        bundle = compile_skills(db)
        reply = bundle['reply']
        texts = reply.get('opening_messages') or [reply.get('opening_message', '')]
        items = delivery_items(reply.get('opening_items'), [t for t in texts if t])
        parts = []
        for item in items:
            info = opening_media_info(db, item, tenant_for_session(db, row)) if item['content_type'] != 'text' else {}
            parts.append({**info, 'content': item.get('content', ''), 'content_type': item['content_type']})
        value['opening_started'] = True
        append_plan(row, value, parts, 'opening', reply.get('opening_interval_seconds', 2))
    save(row, value)
    row.due_at = utcnow() if value.get('pending_event') else None


def append_plan(row, value, parts, kind, interval, run_id=None):
    if not parts:
        return
    plan_id = uuid4().hex
    messages = list(row.messages)
    for index, part in enumerate(parts):
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


def parts_for(db, row, bundle, decision):
    route = decision['route_variant'] or row.controls.get('route_variant', '')
    if route and route not in bundle['routes']:
        raise ValueError('v3_unknown_route')
    parts = []
    tenant_id = tenant_for_session(db, row)
    materials = {m['key']: m for m in candidate_materials(db, tenant_id)}
    def add(text, keys, mode='text_then_assets'):
        media = []
        for key in keys:
            # Comparing routes may reference a different route's image without
            # changing the customer's selected route.
            asset_routes = [r for r in materials.get(key, {}).get('routes', []) if r in bundle['routes']]
            asset_route = route if route in asset_routes or not asset_routes else asset_routes[0]
            media.extend(resolve_materials(db, [key], asset_route, tenant_id))
        media_parts = [{**m, 'content': ''} for m in media]
        text_parts = [{'content': text, 'content_type': 'text'}] if text else []
        parts.extend(media_parts + text_parts if mode == 'assets_then_text' else text_parts + media_parts)
    available = scripts(bundle, route)
    for message in decision['messages']:
        script = available.get(message['script_id']) if message['script_id'] else None
        if message['script_id'] and not message['text'] and script is None:
            raise ValueError('v3_unknown_script')
        add(message['text'] or (script['text'] if script else ''),
            message['asset_keys'])
    if decision['start_introduction']:
        if not route:
            raise ValueError('v3_introduction_requires_route')
        spec = bundle['routes'][route]
        for key in spec['introduction_sequence']:
            group = spec['groups'][key]
            add(group['text'], group['assets'], group.get('delivery_mode', 'assets_then_text'))
    return route, parts


def apply_decision(db, row, run, bundle, decision):
    value = state(row)
    # Resolve references before changing state, so a missing image cannot leave
    # a half-installed plan or be recorded as delivered.
    route, parts = parts_for(db, row, bundle, decision)
    row.memory = {**(row.memory or {}), **decision['profile']}
    if decision['opt_out'] is True:
        value['opt_out'] = True
    if decision['interrupt'] or decision['action'] == 'handoff':
        row.messages = [{**m, 'status': 'cancelled'} if m.get('status') == 'draft' else m for m in row.messages]
        value['delivery_kind'] = None
        value['delivery_due_at'] = None
    if value.get('delivery_kind') == 'introduction' and not decision['interrupt'] and decision['action'] != 'handoff':
        value['pending_event'] = None
        row.due_at = None
        save(row, value)
        return
    if route:
        row.controls = {**row.controls, 'route_variant': route}
    if decision['action'] == 'handoff':
        value['handoff'] = True
        value['handoff_reason'] = decision['handoff_reason']
        row.controls = {**row.controls, 'human': True}
    value['pending_event'] = None
    value['buffered_questions'] = []
    value['next_check_at'] = None
    step = value.get('silence_step', 0) + (1 if run.input_snapshot.get('event') == 'silence_due' else 0)
    value['silence_step'] = step
    intervals = bundle['silence'].get('intervals_minutes', [])
    configured_delay = intervals[step] if step < len(intervals) else None
    stopped = value.get('opt_out') or value.get('handoff') or decision.get('stop_followup') or not bundle['silence']['enabled']
    value['next_check_minutes'] = None if stopped else (decision['next_check_minutes'] or configured_delay)
    value['last_reason'] = decision['reason']
    kind = 'introduction' if decision['start_introduction'] else 'reply'
    if decision['start_introduction']:
        value['introduction_route'] = route
    interval = bundle['routes'].get(route, {}).get('interval_seconds', bundle['reply']['opening_interval_seconds'])
    append_plan(row, value, parts, kind, interval, run.id)
    if run.input_snapshot.get('event') == 'silence_due':
        row.messages = [{**m, 'proactive': True} if m.get('run_id') == run.id else m for m in row.messages]
    if not parts and value['next_check_minutes']:
        value['next_check_at'] = later(row.virtual_now, value['next_check_minutes'] * 60)
    save(row, value)
    row.due_at = None


def complete_delivery(row, target, status):
    value = state(row)
    part = next(m for m in row.messages if m.get('id') == target)
    row.messages = [{**m, 'status': status, 'created_at': row.virtual_now,
                     'confirmed_at': row.virtual_now} if m.get('id') == target else m for m in row.messages]
    if part.get('plan_id') != value.get('delivery_plan_id'):
        return
    if any(m.get('status') == 'draft' for m in row.messages):
        value['delivery_due_at'] = later(row.virtual_now, value['delivery_interval'])
    else:
        kind = value.pop('delivery_kind', None)
        value['delivery_due_at'] = None
        if kind == 'introduction':
            value['completed_introductions'] = list(dict.fromkeys([
                *value.get('completed_introductions', []), value.get('introduction_route')]))
            value['pending_event'] = 'introduction_completed'
            row.due_at = utcnow()
        elif kind != 'opening' and value.get('next_check_minutes'):
            value['next_check_at'] = later(row.virtual_now, value['next_check_minutes'] * 60)
    save(row, value)


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
        if not deliver:
            save(row, value)
            return True
        if date(value['delivery_due_at']) > date(row.virtual_now):
            return True
        target = drafts[0]['id']
        complete_delivery(row, target, 'simulated_delivered')
        value = state(row)
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
        value.update(pending_event=None, delivery_kind=None, delivery_due_at=None, next_check_at=None)
        save(row, value)


def advance_next(row):
    value = state(row)
    if value.get('pending_event') or any(m.get('status') == 'draft' for m in row.messages):
        raise ValueError('journey_busy')
    if not value.get('next_check_at'):
        raise ValueError('journey_next_touch_missing')
    row.virtual_now = value['next_check_at']
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
        bundle = compile_skills(db)
        if event == 'silence_due':
            silence = bundle['silence']
            if value.get('opt_out') or value.get('handoff') or not silence['enabled'] or (environment == 'live' and not silence.get('live_enabled')):
                value.update(pending_event=None, next_check_at=None)
                save(row, value)
                db.commit()
                continue
            local = date(row.virtual_now).astimezone(ZoneInfo('Asia/Taipei'))
            clock = local.strftime('%H:%M')
            start, end = silence.get('active_start', '00:00'), silence.get('active_end', '23:59')
            allowed = start <= clock < end if start < end else clock >= start or clock < end
            today = {m.get('run_id') for m in row.messages if m.get('proactive') and m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')
                     and date(m['created_at']).astimezone(ZoneInfo('Asia/Taipei')).date() == local.date()}
            if not allowed or len(today) >= silence.get('max_proactive_messages_per_day', 6):
                opening = local.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0)
                if opening <= local:
                    opening += timedelta(days=1)
                value.update(pending_event=None, next_check_at=opening.isoformat())
                save(row, value)
                db.commit()
                continue
        context = {'event': event, 'now': row.virtual_now, 'route_variant': row.controls.get('route_variant', ''),
                   'skills': bundle, 'profile': row.memory, 'state': value,
                   'messages': [m for m in row.messages if m.get('direction') == 'incoming' or m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')],
                   'buffered_questions': [m for m in row.messages if m.get('id') in value.get('buffered_questions', [])],
                   'available_materials': candidate_materials(db, tenant_for_session(db, row))}
        context['website_facts'], context['website_version'] = active_web_facts(
            db, tenant_for_session(db, row), '\n'.join(m.get('content', '') for m in context['messages']
                if m.get('direction') == 'incoming'), environment=environment, candidate_pool=True)
        run = AutomationRun(session_id=row.id, generation=row.generation, module='silence_touch' if event == 'silence_due' else 'reply',
                            idempotency_key=f'v3:{row.id}:{uuid4().hex}', status='processing', input_snapshot=context,
                            lease_until=later(utcnow(), 90))
        # Claim the session generation as well as the event. The API may add a
        # message while this model request is in flight.
        expected = deepcopy(row.controls)
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
            decision, logs, digest = run_agent(context)
            db.refresh(row)
            run = db.get(AutomationRun, run_id)
            run.trace = {'engine_version': 'v3', 'event': event, 'skill_digest': bundle['digest'],
                         'logs': logs, 'request_hash': digest, 'outbound': False,
                         'loaded_skills': logs[-1].get('loaded_skills', {}) if logs else {},
                         'skill_tool_calls': [call for log in logs for call in log.get('tool_calls', [])],
                         **metrics(http_calls)}
            run.decision = decision
            if row.generation != generation or row.controls.get('simulation', {}).get('status') == 'stopped':
                run.status, run.error_code = 'cancelled', 'superseded'
            elif decision['action'] == 'queue' and context['state'].get('delivery_kind') == 'introduction' and state(row).get('delivery_kind') != 'introduction':
                # Delivery can finish while this classification call is running.
                # Keep the buffered questions for the completion turn.
                row.memory = {**(row.memory or {}), **decision['profile']}
                value = state(row)
                value['pending_event'] = 'introduction_completed'
                save(row, value)
                row.due_at = utcnow()
                run.status = 'completed'
            else:
                apply_decision(db, row, run, bundle, decision)
                value = state(row)
                for key in ('last_error', 'retry_at', 'attempts', 'failed_event'):
                    value.pop(key, None)
                save(row, value)
                run.status = 'completed'
        except Exception as exc:
            db.rollback()
            db.refresh(row)
            run = db.get(AutomationRun, run_id)
            run.status, run.error_code = 'failed', str(exc)[:120]
            run.decision = decision or {}
            run.trace = {'engine_version': 'v3', 'event': event, 'outbound': False,
                         'logs': logs or getattr(exc, 'calls', []), **metrics(http_calls)}
            if row.generation == generation:
                value = state(row)
                value['last_error'] = str(exc)[:200]
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
                        handoff(db, row, 'model_failed', str(exc)[:200])
                        value = state(row)
                save(row, value)
        finally:
            calls.reset(meter_token)
        run.completed_at, run.lease_until = utcnow(), None
        db.commit()
        return True
    return False
