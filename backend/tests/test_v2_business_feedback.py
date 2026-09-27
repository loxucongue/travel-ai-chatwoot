"""Regression contracts derived from the September 19 business screenshots."""
import pytest

from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.runtime import _enforce_delivery_contract, _allowed_skill_names, run_v2_agent
from app.reception_v2.tools import execute_tool
from app.reception_v2.skill_registry import SkillRegistry
from app.reception_v2.proactive_policy import evaluate_proactive_eligibility
from app.reception_v2.flow_classifier import infer_route_variant
from app.reception_config import configured_silence_nodes, v2_silence_intervals
from app.sop_schedule import schedule_at
from app.route_packages import ROUTES


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
@pytest.mark.parametrize('topic,fact_id', [
    ('arrival', 'service.peach_arrival'), ('青藏鐵路入藏', 'service.peach_rail'),
    ('集合地點', 'service.peach_arrival'), ('成都入藏函', 'service.peach_permit'),
    ('age', 'service.peach_age'), ('75歲', 'service.peach_age'),
])
def test_feedback_facts_are_retrievable_for_both_routes(route, topic, fact_id):
    facts = execute_tool('get_route_facts', {'route_variant': route, 'topic': topic}, SkillRegistry())['facts']
    assert fact_id in [fact['id'] for fact in facts[:3]]


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_itinerary_tool_and_contract_deliver_selected_route_only(route):
    materials = execute_tool('get_route_materials', {'route_variant': route, 'topic': 'itinerary'}, SkillRegistry())['materials']
    expected = ROUTES[route]['groups']['itinerary_overview']['assets']
    assert [item['key'] for item in materials] == expected
    decision = EvaluationDecision(action='reply', branch=ROUTES[route]['branch'], intent='itinerary', route_variant=route)
    decision.delivery_intent = 'itinerary'
    decision.material_keys = ['routes12-pabongka']
    _enforce_delivery_contract({'available_materials': materials}, decision)
    assert decision.material_keys == expected
    assert decision.covered_content_groups == ['itinerary_overview']


def test_unavailable_itinerary_cannot_be_claimed_as_delivered():
    decision = EvaluationDecision(action='reply', branch='peach_11d', intent='itinerary', route_variant='peach_11d_2027')
    decision.delivery_intent = 'itinerary'
    _enforce_delivery_contract({'available_materials': []}, decision)
    assert decision.action == 'handoff'
    assert decision.handoff_reason == 'requested_material_unavailable'
    assert not decision.material_keys and not decision.covered_content_groups
    assert '無法完整提供' in decision.reply


def test_v2_first_silence_is_one_minute():
    nodes = configured_silence_nodes([{'key': 'old', 'journey_trigger': 'silence_mainline'}], v2_silence_intervals())
    due = schedule_at(nodes[0], customer_added_at=None, enrolled_at='2026-09-20T00:00:00+00:00', last_customer_at=None)
    from datetime import datetime
    assert (datetime.fromisoformat(due) - datetime.fromisoformat('2026-09-20T00:00:00+00:00')).total_seconds() == 60


def test_new_topic_and_route_skill_remain_accessible_after_contact_request():
    assert {'peach-9d-2027', 'peach-11d-2027', 'concern-resolution'} <= _allowed_skill_names('lead_handoff', 'peach_9d_2027')


def test_date_and_two_durations_do_not_prefetch_wrong_route():
    assert infer_route_variant('3月9日出發') == ''
    assert infer_route_variant('9日還是11日') == ''


def test_skipping_silence_preserves_bound_route(monkeypatch):
    import app.reception_v2.runtime as runtime
    monkeypatch.setattr(runtime, '_call', lambda *args: pytest.fail('ineligible silence reached model'))
    decision, _, _, _ = run_v2_agent({'module': 'silence_touch', 'route_variant': 'peach_11d_2027',
                                     'journey': {'stage': 'considering'}, 'customer_text': ''})
    assert decision.route_variant == 'peach_11d_2027'
    assert decision.journey_stage == 'considering'
    assert decision.action == 'no_action'


@pytest.mark.parametrize('stage', ['considering', 'handoff', 'captured'])
def test_silence_does_not_disturb_waiting_or_handed_off_customer(stage):
    result = evaluate_proactive_eligibility({'module': 'silence_touch', 'journey': {'stage': stage}},
                                           {'route_variant': 'peach_9d_2027', 'followup_candidates': ['route.9.scope']})
    assert not result.eligible


def test_prefetch_keeps_tools_available_for_route_switch_and_materials(monkeypatch):
    import json
    import app.reception_v2.runtime as runtime
    payloads = []
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    def call(payload, index):
        payloads.append(payload)
        return {'content': json.dumps({'action': 'reply', 'branch': 'peach_9d', 'intent': 'price', 'v2_events': [],
                'reply': '每人人民幣9,980元。', 'route_variant': 'peach_9d_2027',
                'evidence_refs': ['route.9.price'], 'reception_flow': 'route_detail'})}, {'duration_ms': 0}
    monkeypatch.setattr(runtime, '_call', call)
    decision, _, _, trace = run_v2_agent({'module': 'reply', 'customer_text': '價格', 'route_variant': 'peach_9d_2027'})
    assert payloads[0]['tools']
    assert trace['prefetched_fact_ids']


def test_model_extension_is_sent_without_scope_rewrite(monkeypatch):
    import json
    import app.reception_v2.runtime as runtime
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    replies = iter(['我們在林芝集合，還會去布達拉宮。', '我們在林芝集合，第一天安排接機。'])
    def call(payload, index):
        return {'content': json.dumps({'action': 'reply', 'branch': 'peach_9d', 'intent': 'other', 'v2_events': [],
            'reply': next(replies), 'route_variant': 'peach_9d_2027', 'evidence_refs': ['service.peach_arrival']})}, {'duration_ms': 0}
    monkeypatch.setattr(runtime, '_call', call)
    decision, logs, _, _ = run_v2_agent({'module': 'reply', 'customer_text': '集合', 'route_variant': 'peach_9d_2027'})
    assert decision.reply == '我們在林芝集合，還會去布達拉宮。'
    assert len(logs) == 1


def test_special_arrangement_confirmation_creates_handoff_decision(monkeypatch):
    import json
    import app.reception_v2.runtime as runtime
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr(runtime, '_call', lambda *args: ({'content': json.dumps({
        'action': 'reply', 'branch': 'peach_9d', 'intent': 'other', 'reply': '重慶交付入藏函需要由顧問核對。', 'v2_events': [],
        'handoff_reason': 'knowledge_confirmation_required',
        'route_variant': 'peach_9d_2027', 'evidence_refs': ['service.peach_permit']})}, {'duration_ms': 0}))
    decision, _, _, trace = run_v2_agent({'module': 'reply', 'customer_text': '入藏函在重慶拿嗎', 'route_variant': 'peach_9d_2027'})
    assert decision.action == 'handoff'
    assert decision.handoff_reason == 'knowledge_confirmation_required'
    assert decision.wakeup_action == 'skip'
    assert 'confirmation_questions' not in trace


@pytest.mark.parametrize('delivered,expected', [(False, []), (True, ['route.shared.peach_highlights', 'route.shared.landmarks', 'route.shared.zhaji'])])
def test_proactive_photos_require_confirmed_itinerary_receipt(delivered, expected):
    from app.reception_v2.journey_memory import build_journey_memory
    memory = build_journey_memory({'module': 'silence_touch', 'route_variant': 'peach_9d_2027',
        'customer_text': '想看行程', 'journey': {'content_progress': {'itinerary_overview': {
            'asset_keys': ['routes12-9d-itinerary'] if delivered else [], 'history_unknown': False,
        }}}})
    if delivered:
        assert set(expected) <= set(memory['followup_candidates'])
    else:
        assert memory['followup_candidates'] == []


@pytest.mark.parametrize('asset,allowed', [('routes12-pabongka', True), ('routes12-potala', False)])
def test_proactive_generation_is_scoped_to_unsent_value(monkeypatch, asset, allowed):
    import json
    import app.reception_v2.runtime as runtime
    from app.deepseek_evaluation import EvaluationCallError
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr(runtime, '_call', lambda *args: ({'content': json.dumps({
        'action': 'reply', 'branch': 'peach_9d', 'intent': 'other', 'reply': '這張是沿線桃花景點照片。', 'v2_events': [],
        'route_variant': 'peach_9d_2027', 'evidence_refs': ['route.shared.peach_highlights'],
        'content_group_key': 'peach_highlights', 'material_keys': [asset], 'wakeup_action': 'generate'})}, {'duration_ms': 0}))
    context = {'module': 'silence_touch', 'customer_text': '行程', 'route_variant': 'peach_9d_2027',
        'available_materials': [{'key': 'routes12-pabongka'}, {'key': 'routes12-potala'}],
        'journey': {'stage': 'value_building', 'content_progress': {'itinerary_overview': {
            'asset_keys': ['routes12-9d-itinerary'], 'history_unknown': False}}}}
    if allowed:
        decision, _, _, trace = run_v2_agent(context)
        assert decision.material_keys == [asset]
        assert {'route.shared.peach_highlights', 'route.shared.landmarks', 'route.shared.zhaji'} <= set(trace['available_fact_ids'])
        assert 'route.9.overview' not in trace['available_fact_ids']
    else:
        with pytest.raises(EvaluationCallError, match='v2_proactive_material_evidence_mismatch'):
            run_v2_agent(context)
