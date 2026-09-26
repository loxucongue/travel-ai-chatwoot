from copy import deepcopy

import pytest

from app.deepseek_evaluation import EvaluationCallError
from app.reception_config import ReplySettings
from app.reply_fact_verification import FactVerification, _parse
from app.reply_generation import GeneratedReply, call_reply_generator
from app.reply_planning import build_reply_plan
from app.route_reply import automatic_content_already_covered
from app.reply_understanding import CustomerUnderstanding
from app.realtime_reply_pipeline import run_realtime_reply_pipeline
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.route_reply import automatic_content_already_covered
from app.reply_generation import GeneratedFollowUp, _strip_unstructured_follow_up, _strip_contact_benefit_duplication, ASSET_DESCRIPTION_PATTERN
from app.reply_generation import _validate_no_duplicate_asset_narration


def test_complementary_picture_sentences_are_not_duplicate_narration():
    _validate_no_duplicate_asset_narration(
        '這張是扎基寺，行程裡也會安排到訪。除了布達拉宮和八廓街，還能看到更貼近當地生活的寺院人文。',
        ['zhaji'], {'zhaji': '這張是扎基寺～除了布達拉宮和八廓街，行程裡也有更貼近當地生活的寺院人文。'})


def test_advisor_sending_picture_is_not_a_customer_request():
    text = '我把9日行程圖傳給您看。'
    follow_up = GeneratedFollowUp('route_choice', 'route_variant', '您想了解哪一條呢？')
    assert _strip_unstructured_follow_up(text, follow_up) == text


def test_contact_dedup_preserves_itinerary_picture_explanation():
    text = '完整行程圖傳給您，圖中有每天的安排。'
    assert _strip_contact_benefit_duplication(text) == text


@pytest.mark.parametrize('description', ['這份是9日行程總覽圖', '附上路線圖', '這張是客房照片'])
def test_picture_description_accepts_natural_asset_names(description):
    assert ASSET_DESCRIPTION_PATTERN.search(description)


def packet(**changes):
    policy = deepcopy(JOURNEY_POLICY)
    policy['operator_configuration'] = {'opening_message': '您好～您想先了解哪一條行程呢？'}
    result = {
        'module': 'reply', 'customer_text': '你好，我想咨询旅行行程',
        'context_messages': [], 'route_variant': '', 'memory': {},
        'journey': {'sent_content_groups': [], 'customer_profile': {}},
        'lead_capture': {'status': 'not_started'}, 'available_materials': [],
        'reception_policy': policy,
    }
    result.update(changes)
    return result


def test_opening_ignores_incidental_website_hit_and_preserves_published_copy():
    context = packet(global_knowledge_facts=[{'id': 'web.company', 'text': 'Company facts'}])
    plan = build_reply_plan(context, CustomerUnderstanding(intent='route_intro', customer_questions=['itinerary']))
    generated, logs, source = call_reply_generator(context, plan)
    assert generated.reply == context['reception_policy']['operator_configuration']['opening_message']
    assert plan.fixed_answer_scope == 'reception'
    assert plan.allowed_fact_ids == []
    assert generated.asset_ids == [] and logs == []
    assert set(plan.reply_options) == {item['selection_title'] for item in ROUTES.values()}


@pytest.mark.parametrize('topic', ['price', 'medical_service', 'altitude_health', 'weather'])
def test_direct_question_does_not_use_opening(topic):
    plan = build_reply_plan(packet(), CustomerUnderstanding(intent='other', customer_questions=[topic]))
    assert plan.fixed_answer_id != 'unselected_opening'


def test_returning_customer_does_not_receive_opening():
    plan = build_reply_plan(packet(context_messages=[{'direction': 'outgoing', 'content': '您好'}]),
                            CustomerUnderstanding(intent='other', customer_questions=['other']))
    assert plan.fixed_answer_id != 'unselected_opening'


def test_preselected_route_still_starts_first_reception():
    plan = build_reply_plan(packet(route_variant='peach_9d_2027'), CustomerUnderstanding(
        intent='itinerary', customer_questions=['itinerary'], semantic_signals=['general_inquiry']))
    assert plan.allowed_content_group_keys == ['advisor_greeting']


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_route_choice_after_unselected_greeting_starts_full_reception(route):
    context = packet(context_messages=[{'direction': 'outgoing', 'content': '您好，請選擇行程'}])
    plan = build_reply_plan(context, CustomerUnderstanding(
        intent='itinerary', customer_questions=['itinerary'], route_candidate=route,
        route_resolution='confirmed', route_evidence='桃花9日'))
    assert plan.allowed_content_group_keys == ['brand_positioning']
    assert plan.allowed_asset_ids == []
    assert automatic_content_already_covered('advisor_greeting', ['brand_positioning'])
    assert not automatic_content_already_covered('brand_positioning', ['advisor_greeting'])


@pytest.mark.parametrize('topic,expected', [('tips', 'route.shared.tips'), ('medication', 'service.medication'), ('destination_check', 'route.9.scope')])
def test_document_questions_have_specific_evidence_without_price_or_images(topic, expected):
    plan = build_reply_plan(packet(route_variant='peach_9d_2027'), CustomerUnderstanding(intent='other', customer_questions=[topic]))
    assert expected in plan.allowed_fact_ids
    assert 'route.9.price' not in plan.allowed_fact_ids
    assert plan.follow_up is None and plan.allowed_asset_ids == []


def test_medication_with_personal_health_does_not_fall_into_generic_template():
    from app.reply_generation import deterministic_system_reply
    plan = build_reply_plan(packet(route_variant='peach_9d_2027'), CustomerUnderstanding(
        intent='other', customer_questions=['medication'], semantic_signals=['personal_health_suitability']))
    assert 'service.medication' in plan.allowed_fact_ids
    assert deterministic_system_reply(plan) is None


def test_comparative_word_is_rejected_not_mechanically_replaced():
    from app.reply_generation import _normalize_taiwan_service_terms, _validate_customer_visible_body
    text = '請醫師評估會比較妥當。'
    assert _normalize_taiwan_service_terms(text) == text
    with pytest.raises(ValueError, match='reply_must_use_taiwan_service_terms'):
        _validate_customer_visible_body(text)


def test_altitude_question_includes_real_arrangements_and_medical_boundary():
    plan = build_reply_plan(packet(route_variant='peach_9d_2027'), CustomerUnderstanding(intent='other', customer_questions=['altitude_health']))
    assert {'route.9.overview', 'service.safety'} <= set(plan.allowed_fact_ids)
    assert plan.follow_up is None


def test_opening_validates_against_effective_character_limit():
    with pytest.raises(ValueError, match='opening_message_exceeds_reply_limit'):
        ReplySettings(opening_message='x' * 81, max_characters=80)
    with pytest.raises(ValueError):
        ReplySettings(opening_message='   ')


def test_legacy_progress_does_not_restart_introductions_or_duplicate_hotel():
    sent = ['itinerary_overview', 'hotel_reference']
    for key in ['advisor_greeting', 'brand_positioning', 'accommodation_summary']:
        assert automatic_content_already_covered(key, sent)
    assert not automatic_content_already_covered('vehicle_reference', sent)
    assert not automatic_content_already_covered('brand_positioning', ['advisor_greeting'])
    assert not automatic_content_already_covered('brand_positioning', ['advisor_greeting', 'party_question'])
    assert automatic_content_already_covered('contact_transition', ['read_check'])


def test_health_question_has_no_unrelated_follow_up_or_visual():
    context = packet(route_variant='peach_9d_2027', customer_text='要吃什么药？')
    plan = build_reply_plan(context, CustomerUnderstanding(intent='other', customer_questions=['altitude_health']))
    assert plan.follow_up is None
    assert plan.allowed_asset_ids == []


def test_relevance_failure_is_rewritten_once_then_blocked():
    understanding = CustomerUnderstanding(intent='other', customer_questions=['weather'])
    attempts = []
    def generate(context, plan):
        attempts.append(context)
        return GeneratedReply('這是住宿照片。', None, [], []), [], 'g'
    def verify(*args):
        return FactVerification(True, [], False, ['冷不冷']), [], 'v'
    decision, _, _, trace = run_realtime_reply_pipeline(
            packet(route_variant='peach_9d_2027', customer_text='specific question not covered by fixed examples'), understanding_node=lambda c: (understanding, [], 'u'),
            generation_node=generate, verification_node=verify,
        )
    assert decision.action == 'handoff'
    assert decision.handoff_reason == 'knowledge_verification_required'
    assert '這是住宿照片' not in decision.reply
    assert not decision.material_keys
    assert trace['fact_verification_passed'] is True
    assert trace['question_coverage_passed'] is False
    assert len(attempts) == 2
    assert attempts[1]['reply_generation_feedback']['unanswered_questions'] == ['冷不冷']


def test_verifier_distinguishes_correct_facts_from_answering_question():
    result = _parse({'supported': True, 'unsupported_claims': [], 'relevant': False,
                     'unanswered_questions': ['是否有随团医生']})
    assert result.supported and not result.relevant


def test_opening_configuration_round_trip(authenticated):
    client, csrf = authenticated
    config = client.get('/v1/automation/reception-config').json()['config']
    config['reply']['opening_message'] = '您好～想先了解哪條行程呢？'
    config['reply'].pop('opening_messages', None)
    response = client.put('/v1/automation/reception-config', json=config, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.text
    assert client.get('/v1/automation/reception-config').json()['config']['reply']['opening_message'] == config['reply']['opening_message']
