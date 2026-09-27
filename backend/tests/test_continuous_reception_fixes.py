import json

import pytest

from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.events import validate_events, merge_events
from app.reception_v2 import runtime
from app.route_packages import ROUTES


def test_contact_consent_has_no_appointment_and_preserves_optout():
    events = validate_events([{'type': 'contact_agreed', 'quote': '微信聯絡'}],
                             {'customer_text': '微信聯絡'})
    state = merge_events({'_v2_state': {'proactive_opt_out': True}}, events)['_v2_state']
    assert 'contact_at' not in state
    assert state['proactive_opt_out']


def test_acceptance_budget_respects_smaller_suite_allocation(tmp_path, monkeypatch):
    from app import model_metering
    path = tmp_path / 'cost.json'
    path.write_text(json.dumps({'limit_cny': .1, 'calls': []}), encoding='utf-8')
    monkeypatch.setattr(model_metering, '_ledger_path', lambda: path)
    with pytest.raises(RuntimeError, match='budget_exhausted'):
        model_metering.begin_request({'model': 'deepseek-v4-flash', 'max_tokens': 120})
    assert json.loads(path.read_text())['calls'] == []


def test_appointment_keeps_timezone_validation():
    with pytest.raises(ValueError, match='timezone_required'):
        validate_events([{'type': 'contact_scheduled', 'quote': '明天', 'contact_at': '2026-10-01T12:00:00'}],
                        {'customer_text': '明天'})


def test_pdf_topic_alias_combines_with_vehicle():
    events = validate_events([
        {'type': 'material_requested', 'quote': 'PDF', 'topic': 'altitude_guide', 'material_kind': 'other'},
        {'type': 'material_requested', 'quote': '車照', 'material_kind': 'vehicle'},
    ], {'customer_text': 'PDF和車照'})
    route = ROUTES['peach_9d_2027']
    available = {k for g in route['groups'].values() for k in g['assets']}
    decision = EvaluationDecision('reply', route['branch'], 'other', reply='資料',
        route_variant='peach_9d_2027', v2_events=events)
    runtime._compile_delivery_contract({'available_materials': [{'key': k} for k in available]}, decision)
    assert decision.action == 'reply'
    guide = route['groups'][route['policies']['post_capture_material_group']]
    assert set(guide['assets']) <= set(decision.material_keys)
    assert set(route['groups']['vehicle_reference']['assets']) <= set(decision.material_keys)
    assert decision.allow_material_resend


def test_capture_does_not_automatically_resend_an_already_delivered_guide():
    route = ROUTES['peach_9d_2027']
    guide = route['groups'][route['policies']['post_capture_material_group']]
    decision = EvaluationDecision('handoff', route['branch'], 'contact', reply='收到微信。',
        route_variant='peach_9d_2027', lead_action='captured', contact_values={'wechat': 'test_guest'})
    runtime._compile_delivery_contract({'module': 'reply', 'journey': {'sent_asset_keys': guide['assets']},
        'available_materials': [{'key': key} for key in guide['assets']]}, decision)
    assert not decision.material_keys
    assert '附給您' not in decision.reply


def test_combined_material_delivery_records_every_material_group():
    from app.decision_service import generate_decision
    route = ROUTES['peach_9d_2027']
    guide_key = route['policies']['post_capture_material_group']
    assets = [*route['groups'][guide_key]['assets'], *route['groups']['vehicle_reference']['assets']]
    model = EvaluationDecision('reply', route['branch'], 'other', reply='兩份資料給您。',
        route_variant='peach_9d_2027', material_keys=assets, covered_content_groups=['vehicle_reference'])
    decision, _, _, _ = generate_decision({'engine_version': 'v2', 'module': 'reply',
        'customer_text': 'PDF和車照都要', 'available_materials': [{'key': key} for key in assets]},
        model_call=lambda _: (model, [], 'fixture'))
    assert guide_key in decision.covered_content_groups


def test_topic_hint_does_not_remove_a_second_requested_attachment():
    from app.decision_service import _validated_route_references
    route = ROUTES['peach_9d_2027']
    keys = [*route['groups']['vehicle_reference']['assets'],
            *route['groups'][route['policies']['post_capture_material_group']]['assets']]
    decision = EvaluationDecision('reply', route['branch'], 'other', reply='两份资料',
        route_variant='peach_9d_2027', content_group_key='vehicle_reference', material_keys=keys)
    _, kept, _, flags = _validated_route_references(decision, {
        'engine_version': 'v2', 'available_materials': [{'key': k} for k in keys]})
    assert kept == keys and not flags


def test_optional_bad_comparison_card_does_not_discard_answer_or_customer_profile():
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'reply': '两条线路的差异', 'v2_events': [],
        'slots': {'route_variant': 'peach_11d_2027', 'party_size': 3},
        'slot_evidence': {'party_size': '3位'},
        'presentations': [{'type': 'route_comparison', 'routes': []}],
    })}, set(), set(), {'customer_text': '我们3位，比较一下'})
    assert decision.reply == '两条线路的差异'
    assert decision.slots == {'party_size': 3}
    assert not decision.presentations


@pytest.mark.parametrize('route_id', ['peach_9d_2027', 'peach_11d_2027'])
def test_ad_entry_preserves_configured_brand_and_completes_fixed_introduction(route_id):
    route = ROUTES[route_id]
    context = {'module': 'reply', 'context_messages': [], 'customer_text': '我們4位，選這條。',
               'available_materials': [{'key': k} for g in route['groups'].values() for k in g['assets']],
               'reception_policy': {'operator_configuration': {'opening_items': [
                   {'key': 'brand', 'content_type': 'text', 'content': '配置品牌原文'},
                   {'key': 'selection-question', 'content_type': 'text', 'content': '選哪條？'}]}}}
    decision = EvaluationDecision('reply', route['branch'], 'other', reply='模型自行摘要',
        route_variant=route_id, slots={'party_size': '4'},
        v2_events=[{'type': 'route_selected', 'quote': '選這條'}])
    runtime._prepare_route_introduction(context, decision)
    runtime._enforce_delivery_contract(context, decision)
    runtime._attach_configured_opening(context, decision)
    assert decision.introduction_delivery
    assert decision.opening_messages == ['配置品牌原文']
    assert not decision.reply_options
    keys = [s['group_key'] for s in decision.v2_delivery_sections]
    assert keys[1] == 'itinerary_overview'
    assert keys[-1] == 'no_shopping'
    assert len(decision.material_keys) > 2


def test_first_selected_route_asks_configured_party_question():
    route = ROUTES['peach_9d_2027']
    decision = EvaluationDecision('reply', route['branch'], 'other', reply='任意介绍',
        route_variant='peach_9d_2027', evidence_refs=['route.9.price'],
        covered_content_groups=['price_reference'], v2_events=[{'type': 'route_selected'}])
    runtime._prepare_route_introduction({'module': 'reply', 'context_messages': []}, decision)
    assert decision.reply == route['groups']['entry_question']['text']
    assert not decision.material_keys
    assert not decision.evidence_refs and not decision.covered_content_groups


@pytest.mark.parametrize('bound', ['', 'peach_9d_2027'])
def test_comparing_another_route_preserves_actual_selection(bound):
    decision = EvaluationDecision('reply', ROUTES['peach_11d_2027']['branch'], 'other', reply='住宿比較',
        route_variant='peach_11d_2027', v2_events=[{'type': 'route_comparison'}])
    runtime._prepare_route_introduction({'module': 'reply', 'route_variant': bound}, decision)
    assert decision.route_variant == bound
    assert not decision.introduction_delivery


@pytest.mark.parametrize('control,expected_count', [('queue', 3), ('stop', 1), ('switch', 1), ('handoff', 1)])
def test_live_intro_queues_questions_but_interrupts_control(session_factory, monkeypatch, control, expected_count):
    import app.live_reply as live
    from test_live_reply import setup, add_itinerary_progress
    from app.live_reply_models import LiveReplyJob
    from app.models import ConversationState, utcnow
    from app.reception_v2 import ENGINE_RELEASE_ID
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        state, job = db.get(ConversationState, 1), db.get(LiveReplyJob, 1)
        state.ai_engine_version = job.engine_version = 'v2'
        state.ai_engine_release_id = job.engine_release_id = ENGINE_RELEASE_ID
        db.commit()
        add_itinerary_progress(db)
    sections = [{'group_key': 'itinerary_overview', 'text': text, 'asset_keys': [],
                 'evidence_refs': [], 'delivery_mode': 'text_only'} for text in ['第一组', '第二组', '最后一组']]
    decision = EvaluationDecision('reply', 'peach_9d', 'itinerary', reply='第一组',
        route_variant='peach_9d_2027', v2_delivery_sections=sections, introduction_delivery=True)
    monkeypatch.setattr(live, 'generate_decision', lambda _: (decision, [], 'test', {}))
    classified = []
    def classify(text, route):
        classified.append(text)
        return control, {'round': 0}
    monkeypatch.setattr('app.reception_v2.introduction_input.classify_introduction_input', classify)
    def new_message(_):
        if not any(m['id'] == 101 for m in fake.messages):
            fake.messages.append({'id': 101, 'created_at': utcnow(), 'message_type': 0,
                                  'content': '住宿呢？', 'private': False})
    monkeypatch.setattr(live.time, 'sleep', new_message)
    live.process_job(1)
    assert len(fake.sent) == expected_count
    assert len(classified) == 1
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == ('submitted' if control == 'queue' else 'blocked')
        if control == 'queue':
            assert job.trace['introduction_queued_input_ids'] == [101]


def test_route_search_hint_does_not_start_an_unconfirmed_introduction():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2.runtime import _prepare_route_introduction, _messages
    from app.reception_v2.skill_registry import SkillRegistry
    context = {'module': 'reply', 'customer_text': '只有9到11天，不想太趕，住宿也希望好一點。',
               'context_messages': [], 'memory': {'party_size': 2}}
    decision = EvaluationDecision(action='reply', branch='peach_11d', intent='other',
        route_variant='peach_11d_2027', reply='可以先比較兩條線路。')
    _prepare_route_introduction(context, decision)
    assert not decision.introduction_delivery
    assert decision.route_variant == '' and decision.branch == 'unclassified'
    prompt = _messages(context, SkillRegistry())[0]['content']
    assert '"bound_route": ""' in prompt
    assert '"route_search_hint": "peach_11d_2027"' in prompt


def test_agent_customer_memory_excludes_internal_route_snapshot():
    from app.reception_v2.skill_registry import SkillRegistry
    prompt = runtime._messages({'module': 'reply', 'customer_text': '那單房差呢？',
        'memory': {'party_size': 4, '_route_snapshot': {'private_dump': 'DO_NOT_DUPLICATE_ROUTE_SNAPSHOT'}}},
        SkillRegistry())[0]['content']
    assert 'DO_NOT_DUPLICATE_ROUTE_SNAPSHOT' not in prompt
    assert '"party_size": 4' in prompt


@pytest.mark.parametrize('minutes', [3, 5, 0])
def test_considering_uses_explicit_model_delay_before_default_day(minutes):
    from datetime import datetime, timedelta
    now = '2026-09-28T01:00:00+08:00'
    text = '稍後再介紹住宿'
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'reply': '好的，稍後再介紹住宿。',
        'wakeup_action': 'defer', 'defer_minutes': minutes,
        'v2_events': [{'type': 'considering', 'quote': text, 'topic': 'hotel'}],
    })}, set(), set(), {'module': 'reply', 'customer_text': text, 'now': now})
    state = merge_events({}, decision.v2_events)['_v2_state']
    assert datetime.fromisoformat(state['reevaluate_at']) == datetime.fromisoformat(now) + timedelta(minutes=minutes or 1440)


def test_future_hotel_acknowledgement_does_not_mark_hotel_delivered():
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'route_variant': 'peach_11d_2027', 'reply': '5分鐘後再介紹住宿。',
        'wakeup_action': 'defer', 'defer_minutes': 5,
        'evidence_refs': ['route.shared.hotel_reference'], 'covered_content_groups': ['hotel_reference'],
        'v2_events': [{'type': 'considering', 'quote': '5分鐘後', 'topic': 'hotel'}],
    })}, {'route.shared.hotel_reference'}, set(), {'module': 'reply', 'customer_text': '5分鐘後再介紹住宿。'})
    assert not decision.evidence_refs and not decision.covered_content_groups


def test_deferred_material_request_does_not_compile_immediate_photos():
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'route_variant': 'peach_11d_2027', 'reply': '5分鐘後再介紹住宿。',
        'wakeup_action': 'defer', 'defer_minutes': 5,
        'v2_events': [{'type': 'material_requested', 'material_kind': 'hotel', 'quote': '5分鐘後'}],
    })}, set(), set(), {'module': 'reply', 'customer_text': '5分鐘後再介紹住宿。'})
    runtime._compile_delivery_contract({'module': 'reply'}, decision)
    state = merge_events({}, decision.v2_events)['_v2_state']
    assert state['reevaluate_at']
    assert not decision.material_keys and not decision.v2_delivery_sections
    assert decision.reply == '5分鐘後再介紹住宿。'


def test_historical_profile_annotation_does_not_block_current_contact():
    events = validate_events([
        {'type': 'profile_updated', 'quote': '改成3位'},
        {'type': 'human_requested', 'quote': '請真人接手'},
    ], {'customer_text': '請真人接手，我的LINE是test_guest。',
        'context_messages': [{'direction': 'incoming', 'content': '我們改成3位，還是11日。'}]})
    assert [e['type'] for e in events] == ['human_requested']


@pytest.mark.parametrize('topic', ['all', 'LINE', 'WhatsApp'])
def test_refusal_accepts_explicit_scope_in_topic(topic):
    events = validate_events([{'type': 'contact_refused', 'quote': '先不要聯絡', 'topic': topic}],
                             {'customer_text': '先不要聯絡'})
    assert events[0]['scope']


def test_comparison_can_deliver_both_maps_without_selecting_a_route():
    from app.decision_service import _validated_route_references
    keys = [key for route in ROUTES.values() for key in route['groups']['itinerary_overview']['assets']]
    context = {'module': 'reply', 'engine_version': 'v2', 'customer_text': '先比較，還沒選定。',
               'available_materials': [{'key': key} for key in keys]}
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'reply': '兩條行程圖給您對照。', 'route_variant': None,
        'material_keys': keys, 'v2_events': [{'type': 'route_comparison', 'quote': '先比較'}],
    })}, set(), set(keys), context)
    runtime._compile_delivery_contract(context, decision)
    _, materials, _, flags = _validated_route_references(decision, context)
    assert materials == keys and not flags
    assert not decision.route_variant and not decision.introduction_delivery


def test_rehearsal_queues_multiple_questions_across_outgoing_messages(session_factory, monkeypatch):
    from app.automation_models import AutomationSession, AutomationRun
    from app.automation_service import add_customer_message, queue_passive, _auto_confirm_journey_drafts
    from app.models import InboxBinding, utcnow
    from sqlalchemy import select
    with session_factory() as db:
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=1, name='test'))
        s = AutomationSession(owner_id=1, inbox_binding_id=1, mode='journey', environment='playground',
            engine_version='v2', virtual_now=utcnow(), controls={'can_reply': True, 'ai_enabled': True,
                'human': False, 'labels': [], 'route_variant': 'peach_9d_2027', 'history_complete': True})
        db.add(s); db.flush()
        run = AutomationRun(session_id=s.id, generation=s.generation, module='reply',
            status='completed', idempotency_key='intro', decision={'introduction_delivery': True})
        db.add(run); db.flush()
        s.messages = [{'id': 'intro-end', 'direction': 'outgoing', 'source': 'ai', 'run_id': run.id,
                       'status': 'draft', 'content': '介绍末段', 'created_at': s.virtual_now}]
        db.commit()
        monkeypatch.setattr('app.reception_v2.introduction_input.classify_introduction_input',
                            lambda *args: ('queue', {}))
        add_customer_message(db, s, '住宿呢？', 'q1'); db.commit()
        add_customer_message(db, s, '含機票嗎？', 'q2'); db.commit()
        assert s.messages[0]['status'] == 'draft'
        assert not s.due_at
        def deliver(db, session, *args, **kwargs):
            session.messages = [{**m, 'status': 'simulated_delivered'} if m['id'] == 'intro-end' else m
                                for m in session.messages]
        monkeypatch.setattr('app.automation_service.confirm_draft', deliver)
        assert _auto_confirm_journey_drafts(db, s)
        db.commit()
        assert queue_passive(db, session_id=s.id)
        pending = db.scalar(select(AutomationRun).where(AutomationRun.status == 'pending'))
        assert pending.input_snapshot['source_message_ids'] == ['q1', 'q2']
        assert pending.input_snapshot['customer_text'] == '住宿呢？\n含機票嗎？'
        assert pending.input_snapshot['context_messages'][0]['content'] == '介绍末段'
