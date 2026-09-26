"""Regress September 22 business transcripts without outbound delivery."""
import pytest

from app.deepseek_evaluation import EvaluationCallError
from app.reply_planning import build_reply_plan
from app.reply_understanding import CustomerUnderstanding, SlotUpdate
from app.route_packages import ROUTES
from app.route_reply import update_content_progress_values, journey_context_from_values
from app.silence_touch_pipeline import run_silence_touch_pipeline
from test_split_silence_pipeline import completed_initial_context
from test_split_realtime_reply import context
from app.advisor_voice import contact_follow_up_question


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_delivered_materials_and_topics_end_followup_without_model(route):
    source = completed_initial_context(route)
    journey = source['journey']
    slots, sent = journey['slots'], journey['sent_content_groups']
    for key, group in ROUTES[route]['groups'].items():
        if key not in {'read_check', 'contact_transition', 'contact_request'}:
            slots, sent, _ = update_content_progress_values(
                route, slots, sent, key, delivered_text=group['text'], asset_keys=group['assets'])
    source['journey'] = journey_context_from_values(route, 'contact_requested', slots, sent)
    source['touch_index'] = 6
    source['lead_capture'] = {'status': 'asked'}
    def forbidden(*_):
        raise AssertionError('Exhausted value must not call model')
    decision, logs, _, trace = run_silence_touch_pipeline(source, generation_node=forbidden)
    assert decision.action == 'no_action'
    assert not decision.reply and not decision.material_keys and not logs
    assert trace['skip_reason'] == 'silence_no_relevant_content'


def test_duplicate_rejection_is_observable_skip_and_not_delivery():
    attempts = [{'node': 'silence_generation', 'error': 'reply_repeats_recent_advisor_message'}]
    def duplicate(*_):
        raise EvaluationCallError('reply_repeats_recent_advisor_message', attempts, 'rejected')
    decision, logs, _, trace = run_silence_touch_pipeline(completed_initial_context(), generation_node=duplicate)
    assert decision.action == 'no_action'
    assert not decision.reply and not decision.material_keys and not decision.covered_content_groups
    assert logs == attempts
    assert trace['skip_reason'] == 'silence_duplicate_skipped'


def test_network_errors_remain_errors():
    def failed(*_):
        raise EvaluationCallError('model_timeout', [], 'timeout')
    with pytest.raises(EvaluationCallError) as exc:
        run_silence_touch_pipeline(completed_initial_context(), generation_node=failed)
    assert exc.value.code == 'model_timeout'


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
@pytest.mark.parametrize('price_question', [False, True])
def test_party_update_does_not_resell_but_mixed_question_is_answered(route, price_question):
    source = completed_initial_context(route)
    packet = context(customer_text='我們4位，多少錢？' if price_question else '4',
                     route_variant=route, journey=source['journey'], memory=source['memory'])
    understanding = CustomerUnderstanding(
        intent='price' if price_question else 'other',
        slot_updates={'party_size': SlotUpdate(4, '4')},
        semantic_signals=['details_provided'],
        customer_questions=['price', 'party_size'] if price_question else ['other'])
    plan = build_reply_plan(packet, understanding)
    if price_question:
        assert 'profile_update_only' not in plan.safety_flags
        assert plan.allowed_fact_ids or plan.fixed_answer_text
    else:
        assert 'profile_update_only' in plan.safety_flags
        assert not plan.fixed_answer_text and not plan.follow_up
        assert not plan.allowed_asset_ids and not plan.allowed_content_group_keys


@pytest.mark.parametrize('options', [{}, {'commercial': True}, {'reminder': True}, {'departure_undecided': True}])
def test_contact_invitation_does_not_offer_delivered_itinerary(options):
    question = contact_follow_up_question('LINE', itinerary_delivered=True, **options)
    assert 'LINE' in question
    assert not any(term in question for term in ('傳給', '整理', '收完整行程'))
