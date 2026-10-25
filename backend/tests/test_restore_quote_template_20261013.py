from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from legacy_quote_fixture import legacy_peach_package

from app.reception_v3.skills import effective_price_scripts
from scripts.restore_quote_template_20261013 import update_package, amount_from_quote, USAGE
from scripts.update_quote_engagement_20261013 import update_package as previous_package

SOURCE = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-packages'


@pytest.mark.parametrize('days,amount', [(9, 10780), (11, 12180)])
def test_restores_full_quote_preserves_amount_and_is_idempotent(days, amount):
    source = legacy_peach_package(days)
    before = previous_package(source)
    result = update_package(before)
    assert update_package(result) == result
    assert before == previous_package(source)
    price = next(s for s in result['fixed_answers'] if s['id'] == 'price')
    assert amount_from_quote(price['answer_text']) == amount
    assert '拼團升級「4-6人小團」優惠價' in price['answer_text']
    assert price['answer_text'].endswith('不含：機票、午晚餐、小費、個人花費')
    assert price['fallback_text'].endswith('不含：機票、午晚餐、小費、個人花費')
    assert '普通报价不主动列' not in price['usage_note']
    assert USAGE in price['usage_note']
    assert price['valid_for_days'] == 30
    assert price['valid_until'] is None and price['valid_from'] is None
    for key in ('price_reference', 'price_deferral'):
        assert result['content_groups'][key]['approved_text'] == price['answer_text']
    # Every change is constrained to quotation copy, validity and the matching fee fact.
    check = deepcopy(result)
    check['fixed_answers'] = before['fixed_answers']
    check['knowledge_facts'] = before['knowledge_facts']
    for key in ('price_reference', 'price_deferral'):
        check['content_groups'][key]['approved_text'] = before['content_groups'][key]['approved_text']
    assert check == before
    assert [s for s in result['fixed_answers'] if s['id'] != 'price'] == [s for s in before['fixed_answers'] if s['id'] != 'price']
    assert [f for f in result['knowledge_facts'] if not f['id'].endswith('.fee_exclusions')] == [f for f in before['knowledge_facts'] if not f['id'].endswith('.fee_exclusions')]


@pytest.mark.parametrize('days,amount,fallback', [(9, '10,780', '10,780'), (11, '12,180', '12,780')])
def test_effective_full_template_keeps_fixed_customer_deadline_and_expiry(days, amount, fallback):
    source = legacy_peach_package(days)
    price = next(s for s in update_package(source)['fixed_answers'] if s['id'] == 'price')
    offer = {'first_quoted_on': '2026-10-13', 'valid_until': '2026-11-12'}
    for day in (13, 14):
        now = datetime(2026, 10, day, tzinfo=ZoneInfo('Asia/Taipei'))
        text = effective_price_scripts([price], now, offer)[0]['answer_text']
        assert amount in text and '截至2026/11/12前報名，限時優惠' in text
        assert '包含：' in text and '不含：' in text
    expired = effective_price_scripts([price], datetime(2026, 11, 13, tzinfo=ZoneInfo('Asia/Taipei')), offer)[0]['answer_text']
    assert fallback in expired and '不含：' in expired and '截至' not in expired


def test_nonstandard_route_amount_is_read_from_configuration():
    source = legacy_peach_package(9)
    price = next(s for s in source['fixed_answers'] if s['id'] == 'price')
    price['answer_text'] = price['answer_text'].replace('10,780', '10,680')
    updated = next(s for s in update_package(source)['fixed_answers'] if s['id'] == 'price')
    assert '10,680' in updated['answer_text'] and '10,780' in updated['fallback_text']


def test_invalid_price_does_not_silently_invent_amount():
    with pytest.raises(ValueError, match='configured_quote_amount_missing'):
        amount_from_quote('價格待確認')
