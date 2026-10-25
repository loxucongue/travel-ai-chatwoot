"""Public quote dates must not advertise the closed December-February product."""
from copy import deepcopy

import pytest

from scripts.configure_quote_availability_20261015 import update_package
from test_tibet_workbook_reconciliation import package


@pytest.mark.parametrize('days', [8, 10])
def test_legacy_quote_excludes_winter_without_changing_price_facts(days):
    source = package(days)
    script = next(s for s in source['fixed_answers'] if s['id'] == 'price_low')
    script['answer_text'] = script['answer_text'].replace('3/1－3/10、10/15－11/30', '10/15－3/10')
    before = deepcopy(source)
    result = update_package(source)
    assert source == before
    assert result['seasonal_pricing'] == before['seasonal_pricing']
    changed = [s for s in result['fixed_answers'] if s not in before['fixed_answers']]
    assert len(changed) == 1
    assert changed[0]['answer_text'] == script['answer_text'].replace('10/15－3/10', '3/1－3/10、10/15－11/30')
    assert update_package(result) == result
    assert '房差' not in changed[0]['answer_text']


def test_other_operator_calendar_and_custom_wording_are_preserved():
    source = package(8)
    source['seasonal_pricing']['advisor_months'] = []
    assert update_package(source) == source
    source['seasonal_pricing']['advisor_months'] = [12, 1, 2]
    next(s for s in source['fixed_answers'] if s['id'] == 'price_low')['answer_text'] = '營運自訂報價'
    assert update_package(source) == source


@pytest.mark.parametrize('days', [8, 10])
def test_release_package_does_not_reintroduce_nominal_winter_sale_dates(days):
    source = package(days)
    text = next(s for s in source['fixed_answers'] if s['id'] == 'price_low')['answer_text']
    assert '淡季出發：3/1－3/10、10/15－11/30' in text
    assert '10/15－3/10' not in text
