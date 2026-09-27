"""Business regressions found by the full September 20 review."""
from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.journey_state_machine import guard_decision_stage
from app.reception_v2.runtime import _messages, _enforce_delivery_contract
from app.reception_v2.skill_registry import SkillRegistry
from app.reception_v2.tools import execute_tool
from app.reception_config import v2_silence_intervals
import pytest
from types import SimpleNamespace
from app.reception_v2.events import validate_events, merge_events
from app.reception_v2.scheduling import defer_job


def test_question_cannot_create_considering_without_customer_event():
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other',
                                  journey_stage='considering')
    guard_decision_stage(decision, 'value_building')
    assert decision.journey_stage == 'value_building'


def test_normal_second_touch_is_inside_existing_channel_window():
    assert sum(v2_silence_intervals()) < 23 * 60 + 55


def test_operator_guidance_reaches_v2():
    context = {'module': 'reply', 'customer_text': '你好', 'reception_policy': {
        'operator_configuration': {'custom_guidance': 'BUSINESS_GUIDANCE_SENTINEL'}}}
    assert 'BUSINESS_GUIDANCE_SENTINEL' in _messages(context, SkillRegistry())[0]['content']


def test_material_request_survives_missing_optional_delivery_intent():
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='itinerary',
                                  route_variant='peach_9d_2027')
    _enforce_delivery_contract({'available_materials': [{'key': 'routes12-9d-itinerary'}]}, decision)
    assert decision.material_keys == ['routes12-9d-itinerary']


def test_price_tool_includes_approved_discount_and_six_person_price():
    result = execute_tool('get_route_facts', {'route_variant': 'peach_9d_2027', 'topic': 'price'}, SkillRegistry())
    text = ' '.join(f['text'] for f in result['facts'])
    # Website price includes the limited four-to-six-person upgrade.
    price = next(f['text'] for f in result['facts'] if f['id'] == 'route.9.price')
    assert '十人小團優惠價' in price and '9,980/人' in price
    assert '限量升級4-6人小團' in price and '價格不變' in price


def test_hotel_tool_identifies_route_specific_exception():
    result = execute_tool('get_route_facts', {'route_variant': 'peach_9d_2027', 'topic': 'hotel'}, SkillRegistry())
    hotel = next(f['text'] for f in result['facts'] if f['id'] == 'route.shared.hotel_reference')
    assert '地區條件有限' in hotel and '85-90%' in hotel


def test_customer_evidence_required_and_silence_cannot_invent_events():
    with pytest.raises(ValueError, match='evidence_missing'):
        validate_events([{'type': 'considering', 'quote': '考虑一下'}], {'customer_text': '在哪集合'})
    with pytest.raises(ValueError, match='silence_customer_event'):
        validate_events([{'type': 'considering', 'quote': '考虑一下'}], {'module': 'silence_touch'})


def test_valid_considering_event_allows_transition_and_survives_serialization():
    from dataclasses import asdict
    d = EvaluationDecision(action='reply', branch='peach_9d', intent='other', journey_stage='considering')
    d.v2_events = validate_events([{'type': 'considering', 'quote': '和家人商量'}],
                                 {'customer_text': '先和家人商量', 'source_message_id': 42})
    assert guard_decision_stage(d, 'value_building') is None
    assert asdict(d)['v2_events'][0]['source_message_id'] == 42


def test_repeated_question_event_is_idempotent_and_not_marked_answered():
    events = validate_events([{'type': 'question', 'quote': '在哪集合'}],
                             {'customer_text': '在哪集合', 'source_message_id': 7})
    slots = merge_events({}, events)
    assert merge_events(slots, events) == slots
    assert slots['_v2_state']['questions'][0]['status'] == 'pending'


@pytest.mark.parametrize('completed_field', [True, False])
def test_defer_reschedules_same_job_including_last_node(completed_field):
    job = SimpleNamespace(payload={}, status='processing', confirmed_at=None)
    if completed_field:
        job.completed_at = None
    assert defer_job(job, '2026-09-20T02:00:00+00:00', 30)
    assert job.status == 'scheduled'
    assert job.scheduled_at == '2026-09-20T02:30:00+00:00'
    assert job.confirmed_at is None
    for _ in range(5):
        assert defer_job(job, '2026-09-20T02:00:00+00:00', 30)
    assert not defer_job(job, '2026-09-20T02:00:00+00:00', 30)


def test_v2_schedule_rejects_unreachable_configuration():
    from app.reception_config import SilenceSettings
    with pytest.raises(ValueError, match='exceeds_channel_window'):
        SilenceSettings(v2_intervals_minutes=[1, 1440])


def test_failed_receipt_retracts_answer_without_erasing_customer_question():
    from app.reception_v2.events import answer_receipt, rebuild_answers
    events = validate_events([{'type': 'question', 'quote': '多少钱'}],
                             {'customer_text': '多少钱', 'source_message_id': 4})
    slots = merge_events({}, events)
    receipt = answer_receipt({'action': 'reply', 'reply': '9980元起', 'route_variant': 'peach_9d_2027',
                              'v2_events': events, 'evidence_refs': ['route.9.price']}, '9980元起')
    delivered = rebuild_answers(slots, [receipt])
    assert delivered['_v2_state']['questions'][0]['status'] == 'answer_provided'
    assert delivered['_v2_state']['provided_fact_ids']['peach_9d_2027'] == ['route.9.price']
    failed = rebuild_answers(delivered, [])
    assert failed['_v2_state']['questions'][0]['status'] == 'pending'
    assert not failed['_v2_state']['provided_fact_ids']


def test_agreed_contact_defers_until_due_without_consuming_customer_facts():
    from app.reception_v2.proactive_policy import evaluate_proactive_eligibility
    context = {'module': 'silence_touch', 'now': '2026-09-20T02:00:00+00:00',
               'journey': {'stage': 'considering', 'slots': {'_v2_state': {
                   'contact_at': '2026-09-20T12:00:00+08:00'}}}}
    memory = {'route_variant': 'peach_9d_2027', 'followup_candidates': ['route.9.scope']}
    gate = evaluate_proactive_eligibility(context, memory)
    assert not gate.eligible and gate.defer_minutes == 120
    context['now'] = '2026-09-20T04:00:00+00:00'
    assert evaluate_proactive_eligibility(context, memory).eligible


def test_refusing_one_channel_does_not_manufacture_global_opt_out():
    events = validate_events([{'type': 'contact_refused', 'quote': '不要打电话', 'scope': '电话'}],
                             {'customer_text': '不要打电话，用微信'})
    state = merge_events({}, events)['_v2_state']
    assert state['refused_channels'] == ['电话']
    assert not state.get('proactive_opt_out')


def test_paid_reply_budget_is_context_local_and_rejects_late_results(monkeypatch):
    from app.reception_v2 import budget
    clock = [0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    @budget.bounded_turn
    def slow():
        clock[0] = 31
        return 'late'
    from app.deepseek_evaluation import EvaluationCallError
    with pytest.raises(EvaluationCallError, match='deadline'):
        slow()
    assert budget.deadline.get() is None


def test_natural_general_interest_reaches_semantic_followup_selection():
    from app.reception_v2.journey_memory import build_journey_memory
    memory = build_journey_memory({'module': 'silence_touch', 'route_variant': 'peach_9d_2027',
                                    'customer_text': '还有什么好玩的'})
    assert 'route.shared.landmarks' in memory['followup_candidates']


@pytest.mark.parametrize('when,error', [
    ('not-a-date', 'time_invalid'),
    ('2026-09-21T10:00:00', 'timezone_required'),
    ('2026-09-20T01:00:00+00:00', 'time_not_future'),
])
def test_contact_agreement_requires_future_unambiguous_time(when, error):
    with pytest.raises(ValueError, match=error):
        validate_events([{'type': 'contact_agreed', 'quote': '明天联系', 'contact_at': when}],
                        {'customer_text': '明天联系', 'now': '2026-09-20T02:00:00+00:00'})


def test_slot_evidence_cannot_come_from_model_or_history():
    import json
    from app.reception_v2.runtime import _validated_decision
    decision = _validated_decision({'content': json.dumps({
        'action': 'reply', 'reply': '您好', 'slots': {'party_size': 6}, 'v2_events': [],
        'slot_evidence': {'party_size': '六个人'},
    })}, set(), set(), {'customer_text': '你们在哪集合'})
    assert not decision.slots


def test_proactive_itinerary_topic_does_not_replace_cultural_photos_with_itinerary():
    from app.reception_v2.runtime import _enforce_delivery_contract
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='itinerary',
        route_variant='peach_9d_2027', material_keys=['routes12-pabongka'],
        content_group_key='peach_highlights', covered_content_groups=['peach_highlights'])
    _enforce_delivery_contract({'module': 'silence_touch',
        'available_materials': [{'key': 'routes12-9d-itinerary'}]}, decision)
    assert decision.material_keys == ['routes12-pabongka']
    assert decision.covered_content_groups == ['peach_highlights']
