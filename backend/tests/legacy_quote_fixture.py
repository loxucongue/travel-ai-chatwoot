"""Frozen pre-change price inputs for historical migration tests.

Extracted from commit 283b935; current product prices must not rewrite history.
"""
import json
from pathlib import Path


def legacy_peach_package(days):
    root=Path(__file__).resolve().parents[2]
    result=json.loads((root/f'data/knowledge/china2go/route-packages/peach-{days}d-2027/route-package.json').read_text(encoding='utf8'))
    old=json.loads((Path(__file__).with_name('fixtures')/'legacy_peach_quotes_20261013.json').read_text(encoding='utf8'))[str(days)]['price']
    result.pop('party_pricing',None)
    result['fixed_answers']=[old if s['id']=='price' else s for s in result['fixed_answers']]
    for key in ('price_reference','price_deferral'):
        result['content_groups'][key]['approved_text']=old['answer_text']
    return result
