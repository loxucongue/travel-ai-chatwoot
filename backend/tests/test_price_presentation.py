import json
from copy import deepcopy

import pytest

from app.route_packages import PACKAGE_ROOT, _validate
from scripts.update_price_presentation_20261009 import update_package


@pytest.mark.parametrize('days,price', [(9, '10,780'), (11, '12,780')])
def test_brief_quote_preserves_facts_and_unrelated_operator_configuration(days, price):
    path = PACKAGE_ROOT / f'peach-{days}d-2027' / 'route-package.json'
    original = json.loads(path.read_text(encoding='utf8'))
    original['content_groups']['price_reference']['initial_delivery'] = True
    original['initial_delivery_interval_seconds'] = 7
    original['content_groups']['contact_request']['approved_text'] = '自定义邀请'
    updated = update_package(original)
    _validate(deepcopy(updated), path)
    assert updated['knowledge_facts'] == original['knowledge_facts']
    assert updated['content_sequence'] == original['content_sequence']
    assert updated['initial_delivery_interval_seconds'] == 7
    for key in original['content_groups']:
        if key not in ('price_reference', 'price_deferral'):
            assert updated['content_groups'][key] == original['content_groups'][key]
    for key in ('price_reference', 'price_deferral'):
        assert updated['content_groups'][key]['initial_delivery'] is False
    quote = next(s for s in updated['fixed_answers'] if s['id'] == 'price')
    assert quote['answer_text'] == f'這條{days}日行程的標準6人小團是人民幣{price}元/人（兩人一房），人數越多，優惠越多。'
    assert '單房差多少' in quote['negative_examples']
    assert update_package(updated) == updated


def test_unrelated_route_is_not_modified():
    package = {'route_variant': 'other'}
    assert update_package(package) == package
