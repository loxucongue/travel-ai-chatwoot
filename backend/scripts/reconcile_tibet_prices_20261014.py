"""Reconcile the supplied workbook against the inactive four-route release.

Prices in the approved website templates must agree before adding provenance.
This command never imports a live package or opens a database/model connection.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / 'data/knowledge/china2go'
SOURCE = KNOWLEDGE / 'price-sheet-20261014'
RELEASE = KNOWLEDGE / 'route-releases/20261014'
ROWS = {
    'tibet_essence_8d': {'price_low': 3, 'price_spring': 4, 'price_summer': 5, 'price_peak': 6},
    'tibet_overview_10d': {'price_low': 10, 'price_summer': 12, 'price_peak': 13},
}
HEADINGS = {'tibet_essence_8d': ('A1', '希爾頓珠峰8天（每週四）6人團'),
            'tibet_overview_10d': ('A8', '希爾頓珠峰10天（每週二出發）6人團')}


def read_source():
    source = json.loads((SOURCE / 'cells.json').read_text(encoding='utf8'))
    original = SOURCE / source['source_filename']
    if hashlib.sha256(original.read_bytes()).hexdigest() != source['source_sha256']:
        raise ValueError('price_sheet_source_hash_mismatch')
    return source


def reconcile_package(package, source):
    result = deepcopy(package)
    route = result['route_variant']
    if route not in ROWS:
        return result, []
    cells = source['cells']
    heading, expected = HEADINGS[route]
    if cells.get(heading) != expected or source['sheet'] != '西藏':
        raise ValueError('price_sheet_route_heading_mismatch:' + route)
    ref = 'price-sheet-20261014/' + source['source_filename'] + '#西藏!'
    evidence = []
    bands = result['seasonal_pricing']['bands']
    if {b['script_id'] for b in bands} != set(ROWS[route]):
        raise ValueError('price_sheet_unreviewed_band:' + route)
    scripts = {s['id']: s for s in result['fixed_answers']}
    for band in bands:
        row = ROWS[route][band['script_id']]
        price, supplement = cells[f'B{row}'], cells[f'C{row}']
        if (band['price_cny'], band['single_supplement_cny']) != (price, supplement):
            raise ValueError('price_sheet_amount_mismatch:' + route + ':' + band['script_id'])
        script = scripts[band['script_id']]
        # Room supplements remain workbook-backed facts, but ordinary quote
        # templates no longer volunteer them (business feedback 2026-10-15).
        if (f'¥{price:,}/人' not in script['answer_text'] or
                ('單人房差' in script['answer_text'] and f'單人房差：¥{supplement:,}' not in script['answer_text'])):
            raise ValueError('price_sheet_template_mismatch:' + route + ':' + band['script_id'])
        band['source_ref'] = ref + f'A{row}:C{row}'
        script['price_source_ref'] = band['source_ref']
        evidence.append({'route_variant': route, 'script_id': band['script_id'],
                         'cells': f'A{row}:C{row}', 'nominal_period': cells[f'A{row}'],
                         'price_cny': price, 'single_supplement_cny': supplement,
                         'effective_ranges': band['ranges'], 'amounts_match': True})
    result['source']['price_sheet'] = {
        'filename': source['source_filename'], 'sha256': source['source_sha256'],
        'sheet': source['sheet'], 'heading_cell': heading, 'notes_cell': 'A41',
        'precedence': 'Workbook confirms amounts; approved webpage controls sale dates and templates; '
                      '2026-10-14 business clarification treats seasonal closures as alternatives/opt-in retention. '
                      'Holiday exclusions and unavailable dates take precedence over nominal seasons. '
                      'No automatic discount stacking or activation of unsold products.',
    }
    return result, evidence


def main():
    source = read_source()
    prepared, evidence = [], []
    for path in sorted(RELEASE.glob('*/route-package.json')):
        package = json.loads(path.read_text(encoding='utf8'))
        after, rows = reconcile_package(package, source)
        prepared.append((path, after))
        evidence += rows
    if len(evidence) != 7:
        raise ValueError('price_sheet_expected_seven_approved_bands')
    old_routes = []
    for days, row, standard in [(9, 35, 10780), (11, 39, 12780)]:
        package = json.loads((KNOWLEDGE/f'route-packages/peach-{days}d-2027/route-package.json').read_text(encoding='utf8'))
        answer = next(s for s in package['fixed_answers'] if s['id'] == 'price')
        if source['cells'][f'B{row}'] != standard or f'{standard:,}' not in answer.get('fallback_text', answer['answer_text']):
            raise ValueError('price_sheet_peach_standard_mismatch')
        old_routes.append({'route_variant': package['route_variant'], 'cells': f'A{row}:C{row}',
                           'standard_price_cny': standard, 'single_supplement_cny': source['cells'][f'C{row}'],
                           'promotion_preserved': True, 'valid_for_days': answer.get('valid_for_days'),
                           'source_package_sha256': hashlib.sha256((KNOWLEDGE/f'route-packages/peach-{days}d-2027/route-package.json').read_bytes()).hexdigest()})
    report = {'source': {k: v for k, v in source.items() if k != 'cells'},
              'new_route_bands': evidence, 'existing_routes': old_routes,
              'price_changes': 0, 'production_changed': False,
              'decisions': [
                  {'cells': 'A11:C11', 'result': '10日春季11,980/单房差3,000已有依据，但表注桃花节不卖；网页明确3/11–4/30不售，继续禁用该档自动报价。'},
                  {'cells': 'A5:C6;A12:C13;A41', 'result': '工作表为名义季节；网页已避开长假，保留5/6起、9/24止，不开放5/1–5/5或9/25–10/14的普通价。'},
                  {'cells': 'A4:C5', 'result': '2026-10-14业务已澄清：8日4/21–4/24属于季节性未开放，推荐同期已开放产品或邀请登记开放通知；4/25起长假另计仍需确认。'},
                  {'cells': 'A29:C31', 'result': '冬游冰川8日价格已确认9,280/单房差1,500；不是本次精华8日产品，仍缺完整资料和图片，不自动增开。未启用产品以替代行程或开放通知承接，实际留资后顾问接收。'},
                  {'cells': 'A41', 'result': '允许按当地情况调整行程顺序但不减少景点；8日第2/3天先后不再视为缺失报价资料，酒店分店差异仍待核实。'},
                  {'cells': 'A41', 'result': '早鸟与多人优惠可叠加，但折扣计价单位、重叠月份边界及与现有12,180活动叠加关系未明确；不自动计算最终折后价。'},
                  {'cells': 'A35:C39', 'result': '桃花9日标准10,780、11日标准12,780确认；保留已批准11日12,180活动价及首次报价30天固定期限。'},
              ]}
    # All validation happens before any release file is changed.
    for path, package in prepared:
        path.write_text(json.dumps(package, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    target = RELEASE/'price-sheet-reconciliation.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(json.dumps({'verified_seasonal_bands': len(evidence), 'existing_routes': len(old_routes),
                      'price_changes': 0, 'production_changed': False}))


if __name__ == '__main__':
    main()
