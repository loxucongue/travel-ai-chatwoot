"""Apply independently grounded state repairs without regenerating valid copy."""
from copy import deepcopy


def rejected_task_action_patch(decision, audit):
    """Keep copy and action consistent with independent task-demand proof.

    This is a proposed edit only. Runtime re-audits the changed copy and action
    together before creating any persistent task or delivery.
    """
    scope=audit.scope_check or {}
    checks=scope.get('task_scope_checks') or []
    if (decision.handoff_reason!='knowledge_confirmation_required'
            or decision.lead_action not in {'none','',None}
            or not checks or any(c.get('needed') is not False for c in checks)
            or scope.get('consultant_tasks') or audit.confirmation_questions
            or scope.get('event_errors')
            or any(e.get('type') in {'human_requested','contact_agreed','contact_provided'} for e in decision.v2_events)):
        return {}
    return {'action':'reply','handoff_reason':None,'journey_stage':'value_building',
            'lead_action':'none','wakeup_action':None}


def _repair_material_events(current, raw, scope):
    checks=scope.get('material_request_checks') or []
    if not checks or any(c.get('kind') not in {'itinerary','full_introduction','hotel','vehicle','altitude','other'}
                         or not c.get('quote') or c['quote'] not in current for c in checks):
        return None
    revised=deepcopy(raw)
    events=list(revised.get('v2_events') or [])
    kinds={e.get('material_kind') for e in events if e.get('type')=='material_requested'}
    if 'full_introduction' in kinds:
        kinds.update({'itinerary','hotel','vehicle'})
    for check in checks:
        if check['kind'] not in kinds:
            events.append({'type':'material_requested','material_kind':check['kind'],
                           'quote':check['quote'],'topic':check['kind']})
            kinds.add(check['kind'])
    # A independently proved pure material command must not preserve a false
    # question event that would duplicate the compiler's service receipt.
    questions=scope.get('question_checks') or []
    for question in questions:
        quote=question.get('request_quote')
        if (question.get('request_kind')=='fact' and quote and quote in current
                and not any(e.get('type')=='question' and quote in str(e.get('quote') or '') for e in events)):
            events.append({'type':'question','quote':quote,'topic':'current_question'})
    if questions and all(q.get('request_kind')=='material' for q in questions):
        material_quotes={c['quote'] for c in checks}
        events=[e for e in events if not (e.get('type')=='question' and e.get('quote') in material_quotes)]
    if events==list(raw.get('v2_events') or []):
        return None
    revised['v2_events']=events
    return revised


def _repair_service_copy(raw, decision, scope):
    """Keep proved factual answer spans and the one compiler-owned receipt."""
    receipts={
        'requested_material_unavailable':'這份資料目前無法完整提供，我會請顧問協助補齊。',
        'lead_captured':'聯絡方式已收到，我會請顧問接續協助，並補給您高原行前資料。',
        'customer_contact_outside_window':'我會把您希望的聯繫時間交給顧問安排，這段時間先不打擾您。',
    }
    receipt=receipts.get(getattr(decision,'handoff_reason',None))
    body=getattr(decision,'reply_body','') or getattr(decision,'reply','') or ''
    questions=[q for q in scope.get('question_checks',[]) if q.get('request_kind')=='fact']
    if (not receipt or not body.endswith(receipt) or not questions
            or scope.get('event_errors') or scope.get('missing_answers')
            or getattr(decision,'v2_delivery_sections',None)
            or not all(q.get('covered') and q.get('answer_segment_ids') for q in questions)):
        return None
    from app.reception_v2.reply_scope import answer_segments
    segments=answer_segments(body)
    refs={ref for q in questions for ref in q['answer_segment_ids']}
    if not refs <= {s['id'] for s in segments}:
        return None
    answer=''.join(s['text'] for s in segments if s['id'] in refs and s['text'] not in receipt)
    candidate=answer+'\n\n'+receipt
    if not answer or candidate==body:
        return None
    # This is only a candidate edit; runtime always rechecks facts, conditions,
    # every current question and the resulting execution before delivery.
    return {**deepcopy(raw),'reply':candidate,'reply_body':candidate}


def revise_grounded_state(context, raw, decision, audit):
    scope=audit.scope_check or {}
    if context.get('module') in {'silence_touch','wakeup'}:
        return None
    current=str(context.get('customer_text') or '')
    updates=scope.get('profile_update_checks') or []
    missing=[item for item in updates if not item.get('persisted_correctly')]
    if missing:
        # This is a candidate repair, never acceptance: normal validation and a
        # fresh independent audit still run under the existing turn deadline.
        if any(item.get('field') not in {'party_size','departure_window'}
               or not isinstance(item.get('value'),str) or not item['value'].strip()
               or not item.get('quote') or item['quote'] not in current for item in missing):
            return None
        revised=deepcopy(raw)
        slots=dict(revised.get('slots') or {})
        evidence=dict(revised.get('slot_evidence') or {})
        events=list(revised.get('v2_events') or [])
        for item in missing:
            slots[item['field']]=item['value']
            evidence[item['field']]=item['quote']
            if not any(e.get('type')=='profile_updated' and e.get('quote')==item['quote'] for e in events):
                events.append({'type':'profile_updated','quote':item['quote'],'topic':item['field']})
        revised.update(slots=slots,slot_evidence=evidence,v2_events=events)
        return _repair_material_events(current,revised,scope) or revised
    material_revision=_repair_material_events(current,raw,scope)
    if material_revision is not None:
        return material_revision
    from app.reception_v2.reply_revision import verified_profile_ack
    profile_ack=verified_profile_ack(decision,audit)
    if profile_ack:
        patch=profile_ack[0]
        if any((getattr(decision,key,None) or '')!=(value or '')
               for key,value in patch.items() if key!='follow_up_type') or getattr(decision,'follow_up_type','') not in {'',None,'none'}:
            return {**deepcopy(raw),**patch,'reply_body':patch['reply']}
    service_revision=_repair_service_copy(raw,decision,scope)
    if service_revision is not None:
        return service_revision
    # Pure file requests have no independent textual answer. A speculative
    # question event must not prepend a duplicate essay to the approved package.
    if (scope.get('material_request_checks') and not scope.get('event_errors')
            and not scope.get('missing_answers') and not scope.get('contact_refusals')
            and not scope.get('consultant_tasks')
            and not any(q.get('request_kind','fact')=='fact' for q in scope.get('question_checks',[]))
            and any(e.get('type')=='question' for e in decision.v2_events)
            and all(e.get('type') in {'question','material_requested','profile_updated'} for e in decision.v2_events)
            and decision.action=='reply' and decision.lead_action=='none' and not decision.handoff_reason):
        revised=deepcopy(raw)
        revised['v2_events']=[e for e in revised.get('v2_events',[]) if e.get('type')!='question']
        return revised
    return None
