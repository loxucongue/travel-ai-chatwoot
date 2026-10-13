from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.reception_v3.skills import effective_price_scripts
from scripts.update_followup_offer_20261013 import update_package as offer_package
from scripts.update_quote_engagement_20261013 import update_package, USAGE

SOURCE = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-packages'


@pytest.mark.parametrize('days', [9, 11])
def test_quote_migration_preserves_all_other_facts_scripts_and_configuration(days):
    package = json.loads((SOURCE / f'peach-{days}d-2027/route-package.json').read_text(encoding='utf8'))
    package['knowledge_facts'] = [f for f in package['knowledge_facts'] if not f['id'].endswith('.fee_exclusions')]
    price = next(s for s in package['fixed_answers'] if s['id'] == 'price')
    price['answer_text'] += '\n\n不含：機票、午晚餐、小費、個人花費'
    if price.get('fallback_text'):
        price['fallback_text'] += '\n\n不含：機票、午晚餐、小費、個人花費'
    before = deepcopy(package)
    result = update_package(package)
    assert package == before
    assert update_package(result) == result
    updated_price = next(s for s in result['fixed_answers'] if s['id'] == 'price')
    assert '不含：' not in updated_price['answer_text']
    assert '不含：' not in updated_price.get('fallback_text', '')
    assert USAGE in updated_price['usage_note']
    assert result['knowledge_facts'][:-1] == package['knowledge_facts']
    assert '個人花費' in result['knowledge_facts'][-1]['text']
    # Restoring only the intended fields must produce the exact original package.
    for key in ('answer_text', 'fallback_text', 'usage_note'):
        if key in price:
            updated_price[key] = price[key]
        else:
            updated_price.pop(key, None)
    for key in ('price_reference', 'price_deferral'):
        result['content_groups'][key]['approved_text'] = package['content_groups'][key]['approved_text']
    result['knowledge_facts'] = package['knowledge_facts']
    assert result == package


@pytest.mark.parametrize('rolling', [False, True])
@pytest.mark.parametrize('day,amount', [('2026-10-13', '12,180'), ('2026-11-13', '12,780')])
def test_effective_quote_omits_exclusions_and_preserves_operator_usage(rolling, day, amount):
    package = json.loads((SOURCE / 'peach-11d-2027/route-package.json').read_text(encoding='utf8'))
    package = update_package(offer_package(package) if rolling else package)
    now = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Taipei'))
    offer = {'first_quoted_on': '2026-10-13', 'valid_until': '2026-11-12'}
    price = next(s for s in effective_price_scripts(package['fixed_answers'], now, offer) if s['id'] == 'price')
    assert amount in price['answer_text']
    assert '不含：' not in price['answer_text']
    assert USAGE in price['usage_note']
    assert '不含：機票' in '\n'.join(f['text'] for f in package['knowledge_facts'])


def test_unrelated_route_is_untouched():
    package = {'route_variant': 'unrelated', 'fixed_answers': []}
    assert update_package(package) == package
