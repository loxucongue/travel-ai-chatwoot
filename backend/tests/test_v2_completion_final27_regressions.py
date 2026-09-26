import pytest


@pytest.mark.parametrize('body,quote,protected', [
    ('9980元起。這個價格適用6人小團。', '這個價格適用6人小團。', True),
    ('9980元起，6人小團。建議先看看車。', '建議先看看車。', False),
    ('9980元起，6人小團。這個價格適用6人小團。', '這個價格適用6人小團。', False),
    ('9980元起，6人小團。', '9980元起，6人小團。', False),
])
def test_scope_pruning_preserves_conditions_of_retained_claim(body, quote, protected):
    from app.reception_v2.reply_scope import preserve_required_fact_conditions
    data = {'proposed_body': body, 'allowed_facts': [{'answer_conditions': [
        {'label': '6人起價', 'when_any_of': ['9980'], 'require_any_of': ['6人']},
    ]}]}
    result = {'unwanted_parts': [quote], 'unwanted_part_checks': [
        {'quote': quote, 'kind': 'unrequested_topic'},
    ]}
    preserve_required_fact_conditions(data, result)
    assert (quote not in result['unwanted_parts']) is protected


@pytest.mark.parametrize('text,accepted', [
    ('團費未含機票', True), ('團費未包含機票', True),
    ('團費包含機票', False), ('是否含機票尚未確認', False),
])
def test_airfare_exclusion_equivalent_and_negative_controls(text, accepted):
    import re
    from test_v2_service_acceptance import CASES
    pattern = next(c[-1][-1] for c in CASES if c[0] == 'flight')
    assert bool(re.search(pattern, text)) is accepted


def test_scope_uses_registered_conditions_when_wire_facts_only_have_id_and_text():
    from app.reception_v2.reply_scope import preserve_required_fact_conditions
    quote = '這個價格是6人小團的起價。'
    data = {'proposed_body': '9980元起，不含機票。' + quote,
            'allowed_facts': [{'id': 'route.9.price', 'text': 'Published price'}]}
    result = {'unwanted_parts': [quote], 'unwanted_part_checks': [{'quote': quote, 'kind': 'unrequested_topic'}]}
    preserve_required_fact_conditions(data, result)
    assert result['unwanted_parts'] == []


def test_customer_voice_prefers_direct_answers_over_defensive_disclaimers():
    from app.advisor_voice import v2_advisor_voice_contract
    contract = v2_advisor_voice_contract()
    assert '不像法務審查稿' in contract
    assert '保留所有影響本輪答案的適用條件，刪除重複免責' in contract
    assert '已知產品價格與適用條件完整時直接報價' in contract


def test_six_person_price_is_direct_and_keeps_its_real_condition():
    from app.fact_conditions import missing_answer_conditions
    from app.route_packages import ROUTES
    fact = next(f for f in ROUTES['peach_9d_2027']['knowledge_facts'] if f['id'] == 'route.9.price')
    assert missing_answer_conditions('9日標準6人小團是人民幣9,980元／人。', fact) == []
    assert missing_answer_conditions('9日是人民幣9,980元／人。', fact)


def test_age_only_scope_rejects_unasked_medical_disclaimer():
    from app.reception_v2.reply_scope import SCOPE_PROMPT
    assert 'The customer did NOT ask for personal medical suitability' in SCOPE_PROMPT
    assert 'Accuracy does not make an unasked disclaimer necessary' in SCOPE_PROMPT
