"""Restore the operator's full quote format without changing route amounts."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.update_business_feedback_20261011 import quotation
from scripts.update_quote_engagement_20261013 import USAGE as PREVIOUS_USAGE

ROUTES = {'peach_9d_2027': 9, 'peach_11d_2027': 11}
USAGE = ('客户主动问团费时，原样使用完整price模板，必须保留线路名、团型、价格、有效期、包含和不含段落。'
         '两条线路使用相同结构，各自读取本线路价格；不自行省略不含项目或改成一句短报价。'
         '模板外不主动追加房差、独团加价或其他线路价格。'
         '报价后仍按通用Skill接一个轻松的问题；同行关系未知时问家人还是朋友，已知则换兴趣问题。')


def amount_from_quote(text):
    match = re.search(r'人民幣\s*¥\s*([\d,]+)\s*/人', text)
    if not match:
        raise ValueError('configured_quote_amount_missing')
    return int(match.group(1).replace(',', ''))


def update_package(package):
    result = deepcopy(package)
    days = ROUTES.get(result['route_variant'])
    if not days:
        return result
    price = next(s for s in result['fixed_answers'] if s['id'] == 'price')
    amount = amount_from_quote(price['answer_text'])
    fallback = amount_from_quote(price.get('fallback_text') or price['answer_text'])
    price['answer_text'] = quotation(days, amount, promotion=True).replace(
        '截至10/31前報名，國慶月優惠', '截至{offer_deadline}前報名，限時優惠')
    price.update(valid_for_days=30, valid_from=None, valid_until=None,
                 fallback_text=quotation(days, fallback))
    note = price.get('usage_note', '').replace(PREVIOUS_USAGE, '').strip()
    price['usage_note'] = note if USAGE in note else (note + '\n' + USAGE).strip()
    for key in ('price_reference', 'price_deferral'):
        if key in result.get('content_groups', {}):
            result['content_groups'][key]['approved_text'] = price['answer_text']
    for fact in result.get('knowledge_facts', []):
        if fact['id'] == result['route_variant'] + '.fee_exclusions':
            fact['text'] = ('团费范围：不含機票、午晚餐、小費、個人花費。普通团费报价按完整price模板列出包含与不含；'
                            '只问具体某项费用时只答对应项目，不重复整份报价。')
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
    updated = {key: update_package(value) for key, value in packages.items()}
    changed = [key for key in packages if packages[key] != updated[key]]
    if args.apply and changed:
        args.backup.mkdir(parents=True, exist_ok=False)
        for key in changed:
            (args.backup / (key + '.json')).write_text(
                json.dumps(packages[key], ensure_ascii=False, indent=2), encoding='utf8')
        for key in changed:
            updated[key]['package_version'] += '.full-quote-20261013'
            install_route_package(updated[key])
    print(json.dumps({'applied': args.apply, 'changed': changed, 'messages_sent': 0}))


if __name__ == '__main__':
    main()
