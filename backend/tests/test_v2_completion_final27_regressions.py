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


def test_source_script_is_not_overridden_by_old_price_or_age_prompt():
    from app.reception_v2.runtime import SYSTEM_PROMPT
    assert '標準6人' not in SYSTEM_PROMPT
    assert '65–75' not in SYSTEM_PROMPT


def test_price_uses_website_offer_including_upgrade():
    from app.route_packages import ROUTES
    fact = next(f for f in ROUTES['peach_9d_2027']['knowledge_facts'] if f['id'] == 'route.9.price')
    assert '十人小團優惠價：¥9,980/人' in fact['text']
    assert '限量升級4-6人小團 價格不變' in fact['text']
