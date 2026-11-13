"""Offline business-source, arithmetic and material integrity acceptance."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
RELEASE = ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing'
PACKAGE = json.loads((RELEASE / 'route-package.json').read_text(encoding='utf8'))
MANIFEST = json.loads((RELEASE / 'asset-manifest.json').read_text(encoding='utf8'))


def test_package_schema():
    from app.route_packages import _validate
    assert _validate(PACKAGE, RELEASE / 'route-package.json')['route_variant'] == 'beijing_micro_5d'


@pytest.mark.parametrize('year', [2026, 2027])
@pytest.mark.parametrize('month', range(1, 13))
@pytest.mark.parametrize('people', range(2, 7))
def test_final_price_matrix(year, month, people):
    season = 'high' if month in (7, 8) else 'low'
    scripts = [s for s in PACKAGE['fixed_answers'] if s['id'].startswith('price_' + season)
               and s['party_size_min'] <= people <= s['party_size_max']]
    assert len(scripts) == 1
    expected = (6280 if month in (7, 8) else 5780) - (400 if people == 6 else 200 if people >= 4 else 0)
    assert f'¥{expected:,}' in scripts[0]['answer_text']
    assert str(year) in scripts[0]['usage_note']
    assert '早鳥' in scripts[0]['usage_note'] or '不可再扣' in scripts[0]['usage_note']


@pytest.mark.parametrize('asset', MANIFEST, ids=lambda a: a['asset_key'])
def test_original_material(asset):
    payload = (RELEASE / asset['path']).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == asset['sha256']
    assert len(payload) == asset['byte_size']
    assert any(asset['asset_key'] in g['asset_keys'] for g in PACKAGE['content_groups'].values())


def test_complete_sections_and_boundaries():
    facts = {f['id'].split('.')[-1]: f['text'] for f in PACKAGE['knowledge_facts']}
    assert all('faq_' + str(n) in facts for n in range(1, 18))
    assert all(k in facts for k in ('intake', 'seasons', 'toboggan', 'family', 'history'))
    assert len(MANIFEST) == 40
    assert len(PACKAGE['content_sequence']) == 9
    assert all(PACKAGE['content_groups'][k]['asset_keys'] for k in PACKAGE['content_sequence'])
    assert '每位客人' in facts['faq_7']
    assert all(s['party_size_min'] >= 2 and s['party_size_max'] <= 6 for s in PACKAGE['fixed_answers'])
    assert not PACKAGE.get('party_pricing') and not PACKAGE.get('seasonal_pricing')
    assert '2028年' in PACKAGE['fixed_answers'][0]['negative_examples']
    assert PACKAGE['followup_policy']['checkpoints']['quote_wait'] == 180
    assert len(PACKAGE['followup_policy']['daily_sequence']) == 6
