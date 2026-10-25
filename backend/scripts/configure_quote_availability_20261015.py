"""Keep the public low-season dates consistent with the approved winter closure."""
from copy import deepcopy
import json
from pathlib import Path


def update_package(package):
    result = deepcopy(package)
    if result.get('route_variant') not in ('tibet_essence_8d', 'tibet_overview_10d'):
        return result
    policy = result.get('seasonal_pricing', {})
    # This migration corrects the existing nominal workbook period only. Do not
    # silently rewrite an operator's different calendar or custom quote wording.
    if set(policy.get('advisor_months', [])) != {12, 1, 2}:
        return result
    for script in result.get('fixed_answers', []):
        if script['id'] == 'price_low':
            script['answer_text'] = script['answer_text'].replace(
                '淡季出發：10/15－3/10', '淡季出發：3/1－3/10、10/15－11/30')
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-releases/20261014'
    for path in root.glob('*/route-package.json'):
        before = json.loads(path.read_text(encoding='utf8'))
        after = update_package(before)
        if after != before:
            path.write_text(json.dumps(after, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print('Updated the two low-season date displays; prices and availability unchanged.')
