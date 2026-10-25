"""Apply the approved quote display scope without changing any price facts."""
from copy import deepcopy
import json
from pathlib import Path
import re

GUIDANCE = ('2026-10-15最新報價口徑：普通團費報價不主動列單人房差；保留團費、兩人一間、包含、不含和報價後的輕鬆問句。'
            '只有客人主動問單住費用、單人房差或相關差額時，才核對出發日期並讀取single_supplement_cny回答；'
            '僅說一位旅客不等於要求單住，不主動補房差。')


def update_package(package):
    result = deepcopy(package)
    if result.get('route_variant') not in ('tibet_essence_8d', 'tibet_overview_10d'):
        return result
    ids = {b['script_id'] for b in result['seasonal_pricing']['bands']}
    for script in result['fixed_answers']:
        if script['id'] not in ids:
            continue
        script['answer_text'] = '\n'.join(
            line for line in script['answer_text'].splitlines()
            if not re.match(r'^\s*單人?房差\s*[:：]', line))
        if GUIDANCE not in script.get('usage_note', ''):
            script['usage_note'] = script.get('usage_note', '') + '\n' + GUIDANCE
        script['answer_origin'] = 'operator_approved'
    if GUIDANCE not in result.get('ai_guidance', ''):
        result['ai_guidance'] = result.get('ai_guidance', '') + '\n' + GUIDANCE
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-releases/20261014'
    for path in root.glob('*/route-package.json'):
        before = json.loads(path.read_text(encoding='utf8'))
        after = update_package(before)
        if after != before:
            path.write_text(json.dumps(after, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print('Updated source quote templates; price facts and live data unchanged.')
