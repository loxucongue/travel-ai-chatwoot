"""Apply the supplied 2027 price sheet to the two peach routes only."""
from copy import deepcopy
import json
from pathlib import Path
import sys

SOURCE = '2027-售價表總表-1002.xlsx#西藏'
PRICES = {'peach_9d_2027': (9, 10780, 2700, 35), 'peach_11d_2027': (11, 12780, 3200, 39)}
PRICE_USAGE = ('使用本线路最新售价表中的6人团、两人一房价格。只问团费不计算全团合计；'
               '单问房差只答本线路单房差。4人独立成团按原价每人加400元，6人独立成团按原价；'
               '未明确独立成团时不自动加价。优惠按最新价格政策回答，未明确的计价单位与时间边界请顾问确认，'
               '不把不同人数、优惠前后价格混用。')


def update_package(package):
    result = deepcopy(package)
    key = result['route_variant']
    if key not in PRICES:
        return result
    days, price, single, row = PRICES[key]
    title = f'西藏桃花節{days}日遊' + ('（不上珠峰）' if days == 9 else '')
    text = (f'【{title}】\n\n出發時間：3/20-4/10（每週五、六、日、一發團）\n'
            f'標準6人小團：人民幣{price:,}元/人（兩人一房，一個人住一間須補單房差人民幣{single:,}元）\n'
            '4人獨立成團每人加400元；6人獨立成團按原價。\n'
            '包含：入藏函，車，導遊，司機，保險，住宿，酒店早餐，門票\n不含：機票、正餐、小費')
    price_id = f'route.{days}.price'
    for fact in result['knowledge_facts']:
        if fact['id'] == price_id:
            fact.update(text=text, source_ref=f'{SOURCE}!A{row-2}:C{row};A41（包含项沿用既有线路资料）')
        elif fact['id'] == 'route.shared.group_offer':
            fact.update(text='標準6人團報價；4人獨立成團每人加400元，6人獨立成團按原價。', source_ref=SOURCE+'!A41')
    policies = {
        f'route.{days}.pricing_policy':
            '最新售价表备注：春節、勞動節、十一團期價格另外計算。獨立成團：4人獨立成團原價+400/人；6人獨立成團原價/人。'
            '早鳥優惠：出發前半年報名-600，出發前3–6個月報名-400，出發前3個月內報名-200。'
            '多人優惠：4人報名-200，6人報名-400，8人報名-600，10人報名-800，以此類推。早鳥和多人優惠可以重疊使用。'
            '表内未写明优惠扣减是每人还是每单，且半年与3–6个月、3个月边界重叠；可说明原表优惠，精确优惠后报价由顾问确认。',
    }
    existing = {f['id']: f for f in result['knowledge_facts']}
    for fid, value in policies.items():
        if fid in existing:
            existing[fid].update(text=value, source_ref=SOURCE+'!A41')
        else:
            result['knowledge_facts'].append({'id':fid,'text':value,'source_ref':SOURCE+'!A41'})
    for gid in ('price_reference', 'price_deferral'):
        if gid in result['content_groups']:
            result['content_groups'][gid]['approved_text'] = text
    for script in result['fixed_answers']:
        if script['id'] == 'price':
            script.update(answer_text=text, usage_note=PRICE_USAGE, source_ref=f'{SOURCE}!A{row-2}:C{row};A41')
        elif script['id'] == 'group_party':
            script['usage_note'] = ('保留公司定位与4–6人团型原话；介绍完成后去掉先介绍行程。'
                                    '自己一团的报价按最新售价表：4人每人加400元、6人按原价；不自动假设客户选择独立成团。')
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
    changes = []
    for route, before in load_route_packages().items():
        after = update_package(before)
        if after != before:
            changes.append((route, before, after))
    if args.apply and changes:
        args.backup.mkdir(parents=True, exist_ok=False)
        for route, before, after in changes:
            (args.backup / (route+'.json')).write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf8')
            after['package_version'] += '.price-1002-20261008'
            install_route_package(after)
    print(json.dumps({'applied':args.apply,'routes':[r for r,_,_ in changes],'prices':PRICES,'messages_sent':0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
