import pytest


@pytest.mark.parametrize('text,accepted', [
    ('團費未含機票', True), ('團費未包含機票', True),
    ('團費包含機票', False), ('是否含機票尚未確認', False),
])
def test_airfare_exclusion_equivalent_and_negative_controls(text, accepted):
    import re
    from test_v2_service_acceptance import CASES
    pattern = next(c[-1][-1] for c in CASES if c[0] == 'flight')
    assert bool(re.search(pattern, text)) is accepted


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
