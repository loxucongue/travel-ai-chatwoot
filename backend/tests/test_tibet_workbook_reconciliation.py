"""Workbook evidence must not accidentally widen availability or overwrite offers."""
from copy import deepcopy
import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.reception_v3.seasonal_quotes import departure_quote
from scripts.reconcile_tibet_prices_20261014 import RELEASE, SOURCE, read_source, reconcile_package


def package(days):
    folder = 'tibet-essence-8d' if days == 8 else 'tibet-overview-10d'
    return json.loads((RELEASE/folder/'route-package.json').read_text(encoding='utf8'))


@pytest.mark.parametrize('days', [8, 10])
def test_workbook_prices_match_actual_xlsx_cells_and_approved_templates(days):
    source = read_source()
    result, evidence = reconcile_package(package(days), source)
    with zipfile.ZipFile(SOURCE/source['source_filename']) as book:
        root = ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    actual = {c.get('r'): c.findtext('s:v', namespaces=ns) for c in root.findall('.//s:c', ns)}
    for band in evidence:
        row = band['cells'].split(':')[0][1:]
        assert int(actual['B'+row]) == band['price_cny']
        assert int(actual['C'+row]) == band['single_supplement_cny']
        script = next(s for s in result['fixed_answers'] if s['id'] == band['script_id'])
        assert '房差' not in script['answer_text']
        assert '包含' in script['answer_text'] and '不含' in script['answer_text']
    again, _ = reconcile_package(result, source)
    assert again == result


@pytest.mark.parametrize('field', ['price_cny', 'single_supplement_cny'])
def test_price_sheet_disagreement_aborts_without_changing_input(field):
    original = package(8)
    original['seasonal_pricing']['bands'][0][field] += 100
    before = deepcopy(original)
    with pytest.raises(ValueError, match='price_sheet_amount_mismatch'):
        reconcile_package(original, read_source())
    assert original == before


def test_eight_day_product_is_not_confused_with_nyingchi_eight_day():
    source = read_source()
    source['cells']['A1'] = source['cells']['A15']
    with pytest.raises(ValueError, match='route_heading_mismatch'):
        reconcile_package(package(8), source)


def test_workbook_does_not_activate_unsold_spring_price():
    source = read_source()
    assert source['cells']['B11'] == 11980
    item, _ = reconcile_package(package(10), source)
    assert 'price_spring' not in {b['script_id'] for b in item['seasonal_pricing']['bands']}
    route = item['route_variant']
    bundle = {'routes': {route: item}}
    assert departure_quote(bundle, route, '2027-03-16', '', '2026-10-14T00:00:00Z')['status'] == 'unavailable'


@pytest.mark.parametrize('days,start,end,status', [
    (8, '2027-05-01', '2027-05-05', 'holiday'),
    (10, '2027-05-01', '2027-05-05', 'holiday'),
    (8, '2027-09-25', '2027-09-30', 'holiday'),
    (10, '2027-09-25', '2027-09-30', 'holiday'),
    (8, '2027-04-22', '', 'unavailable'),
    (8, '2027-12-16', '', 'unavailable'),
    (10, '2027-12-21', '', 'unavailable'),
])
def test_workbook_nominal_seasons_preserve_specific_sale_restrictions(days, start, end, status):
    item, _ = reconcile_package(package(days), read_source())
    route = item['route_variant']
    result = departure_quote({'routes': {route: item}}, route, start, end, '2026-10-14T00:00:00Z')
    assert result['status'] == status
    assert 'price_cny' not in result


@pytest.mark.parametrize('days,row,price', [(9, 35, 10780), (11, 39, 12780)])
def test_workbook_standard_price_preserves_existing_approved_promotion(days, row, price):
    from app.route_packages import PACKAGE_ROOT
    item = json.loads((PACKAGE_ROOT/f'peach-{days}d-2027/route-package.json').read_text(encoding='utf8'))
    after, evidence = reconcile_package(item, read_source())
    assert after == item and evidence == []
    assert read_source()['cells'][f'B{row}'] == price
    if days == 11:
        quote = next(s for s in after['fixed_answers'] if s['id'] == 'price')
        assert '12,180' in quote['answer_text']
        assert '12,780' in quote['fallback_text']
        assert quote['valid_for_days'] == 30
