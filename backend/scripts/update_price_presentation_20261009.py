"""Shorten customer-facing price copy; preserve detailed facts for explicit questions."""
from copy import deepcopy
import json
from pathlib import Path
import sys

PRICES = {'peach_9d_2027': (9, 10780), 'peach_11d_2027': (11, 12780)}
USAGE = ('客户明确问团费时原样使用这段短答。未问价格不主动报；不附加单房差、独立成团加价、'
         '费用包含清单、出发日期或具体折扣。明确追问某项收费时，从线路事实只回答对应那一项。')


def update_package(package):
    result = deepcopy(package)
    if result['route_variant'] not in PRICES:
        return result
    days, price = PRICES[result['route_variant']]
    text = f'這條{days}日行程的標準6人小團是人民幣{price:,}元/人（兩人一房），人數越多，優惠越多。'
    for key in ('price_reference', 'price_deferral'):
        if key in result['content_groups']:
            result['content_groups'][key].update(approved_text=text, purpose=USAGE, initial_delivery=False)
    for script in result['fixed_answers']:
        if script['id'] == 'price':
            script.update(name='團費', answer_text=text, usage_note=USAGE,
                          positive_examples=['一個人多少錢', '團費多少', '這個行程多少錢', f'{days}日多少錢'])
            script['negative_examples'] = list(dict.fromkeys([
                *script.get('negative_examples', []), '單房差多少', '價格包含什麼', '我們4位', '什麼時候出發']))
        elif script['id'] == 'group_party':
            script['usage_note'] = ('保留公司定位与4–6人团型原话；介绍完成后去掉先介绍行程。'
                                    '未问价格不介绍报价或独立成团加价；不假设客户选择独立成团，也不承诺同价。')
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
    changed = []
    for key, before in load_route_packages().items():
        after = update_package(before)
        if after == before:
            continue
        if args.apply:
            args.backup.mkdir(parents=True, exist_ok=True)
            (args.backup / (key+'.json')).write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf8')
            after['package_version'] += '.brief-price-20261009'
            install_route_package(after)
        changed.append(key)
    print(json.dumps({'applied': args.apply, 'routes': changed, 'messages_sent': 0}))


if __name__ == '__main__':
    main()
