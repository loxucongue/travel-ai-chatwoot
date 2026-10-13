"""Update default quote copy only; retain all facts for explicit fee questions."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

ROUTES = ('peach_9d_2027', 'peach_11d_2027')
USAGE = ('客户主动问团费才发送；普通报价不主动列不含项目、房差或其他加价。'
         '问具体费用包含范围或是否全包时按事实回答，不暗示全包。'
         '报价最后按通用Skill接一个轻松的问题，同行关系未知时优先问家人还是朋友；'
         '已知则换相关兴趣问题。尊重明确不要追问的要求。')
EXCLUSIONS = '機票、午晚餐、小費、個人花費'


def without_exclusions(text):
    # Only remove the standalone exclusions line in the approved quote, not facts.
    lines = [line for line in text.splitlines()
             if not re.match(r'^\s*不(?:包含|包括|含)\s*[:：]', line)]
    return '\n'.join(lines).rstrip()


def update_package(package):
    result = deepcopy(package)
    if result['route_variant'] not in ROUTES:
        return result
    fact_id = result['route_variant'] + '.fee_exclusions'
    if not any(f['id'] == fact_id for f in result.get('knowledge_facts', [])):
        result.setdefault('knowledge_facts', []).append({
            'id': fact_id,
            'text': '费用范围事实（仅客户主动问时说明）：團費不含：' + EXCLUSIONS +
                    '。普通报价不主动列这份清单；问是否全包或完整不含明细时如实说明全部四项。',
            'source_ref': 'business-feedback-20261011#quotation-exclusions',
        })
    price = next(s for s in result['fixed_answers'] if s['id'] == 'price')
    for key in ('answer_text', 'fallback_text'):
        if price.get(key):
            price[key] = without_exclusions(price[key])
    if USAGE not in price.get('usage_note', ''):
        price['usage_note'] = price.get('usage_note', '').rstrip() + '\n' + USAGE
    for key in ('price_reference', 'price_deferral'):
        group = result.get('content_groups', {}).get(key)
        if group:
            group['approved_text'] = without_exclusions(group['approved_text'])
    return result


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.route_packages import load_route_packages, install_route_package
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error('--apply requires --backup')
    packages = load_route_packages()
    changes = {key: update_package(value) for key, value in packages.items() if key in ROUTES}
    changed = [key for key, value in changes.items() if value != packages[key]]
    if args.apply and changed:
        args.backup.mkdir(parents=True, exist_ok=False)
        for key in changed:
            (args.backup / (key + '.json')).write_text(
                json.dumps(packages[key], ensure_ascii=False, indent=2), encoding='utf8')
        for key in changed:
            changes[key]['package_version'] += '.quote-engagement-20261013'
            install_route_package(changes[key])
    print(json.dumps({'applied': args.apply, 'changed': changed, 'messages_sent': 0}))


if __name__ == '__main__':
    main()
