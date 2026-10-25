"""Separate private-group date eligibility from scheduled shared departures."""
from copy import deepcopy
import json
from pathlib import Path

GUIDANCE = ('2026-10-15 14:50最新業務口徑：4位及以上可獨立成團自選出發日期，4位以下按定班；'
            '4位以上若明確選擇和其他旅客拼團，仍按拼團定班。定班星期不是獨團的日期限制。'
            '僅有4位不等於已選獨團；可先條件式說明並確認意願，不要求先改日期。'
            '獨團與拼團還有日期和費用差異，不能說只差是否有其他旅客。'
            '自選日期仍受產品季節開放、年份、長假和入藏函期限限制，不代表已確認名額，亦不允許任意改行程或車型。'
            '非定班日返回private_departure只確認可自選日期，不授權套定班報價；只問日期不轉人工，問該日實際獨團總價則靜默交接核對。'
            '4位獨團每人加400的既有條件保留；7位以上仍交人工安排，不用定班星期拒絕。')


def update_package(package):
    result = deepcopy(package)
    if result.get('route_variant') not in ('tibet_essence_8d','tibet_overview_10d','peach_9d_2027','peach_11d_2027'):
        return result
    policy = result.get('seasonal_pricing') or result.get('departure_availability')
    if policy is None:
        raise ValueError('departure_policy_missing:'+result['route_variant'])
    policy['private_departure_min_party_size'] = 4
    if GUIDANCE not in result.get('ai_guidance', ''):
        result['ai_guidance'] = result.get('ai_guidance', '')+'\n'+GUIDANCE
    return result


if __name__ == '__main__':
    root=Path(__file__).resolve().parents[2]/'data/knowledge/china2go'
    paths=list((root/'route-releases/20261014').glob('*/route-package.json'))
    paths += [root/f'route-packages/peach-{days}d-2027/route-package.json' for days in (9,11)]
    prepared=[(p,update_package(json.loads(p.read_text(encoding='utf8')))) for p in paths]
    for p,data in prepared:p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('Updated four source packages; prices and live data unchanged.')
