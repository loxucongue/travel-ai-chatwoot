"""Approved peach quote layout and per-person discounts; caller publishes packages."""
from copy import deepcopy

SOURCE = '2027-售價表總表-1002.xlsx#西藏!A41；2026-10-25用戶確認多人優惠按每人扣減；麗麗2026-10-24報價格式'
PRICES = {'peach_9d_2027': (9, 10780, 10780), 'peach_11d_2027': (11, 12780, 12180)}
INCLUSIONS = '包含：高原保障險、入藏文件、全程供氧住宿（含早餐）、9座VIP供氧車、導遊、門票\n\n不含：機票、午晚餐、小費、個人花費'


def update_package(original):
    package = deepcopy(original)
    if package['route_variant'] not in PRICES:
        return package
    days, base, legacy = PRICES[package['route_variant']]
    title = f'西藏桃花節{days}日遊'
    template = title + '\n拼團升級「4-6人小團」優惠價：人民幣¥ {amount} /人\n\n' + INCLUSIONS
    private = title + '\n獨立成團優惠價：人民幣¥ {amount} /人\n\n' + INCLUSIONS
    package['party_pricing'] = {
        'currency':'CNY', 'base_price_cny':base,
        'discounts_per_person':{'1':0,'2':0,'3':0,'4':200,'6':400},
        'private_supplements_per_person':{'4':400,'6':0},
        'shared_template':template,'private_template':private,
        'legacy_offer_price_cny':legacy,'source_ref':SOURCE,
    }
    price = next(s for s in package['fixed_answers'] if s['id']=='price')
    for key in ('valid_from','valid_until','valid_for_days','fallback_text'):
        price.pop(key,None)
    price.update(answer_text=template.format(amount=f'{base:,}'),source_ref=SOURCE,
                 usage_note='原價參考。實際回覆由party_pricing生成對應明確人數及團型的完整模板；不展示早鳥、限時或折扣拆解。')
    for key in ('price_reference','price_deferral'):
        if key in package['content_groups']:
            package['content_groups'][key]['approved_text']=price['answer_text']
    for fact in package['knowledge_facts']:
        if fact['id'].endswith('.pricing_policy'):
            fact.update(text='春節、勞動節、十一團期價格另外計算。一般日期按原價與已確認人數計算最終每人價：4人報名每人減200元，6人報名每人減400元。'
                        '4人獨團原價每人加400元，6人獨團按原價，再套上述人數優惠。'
                        '不另扣早鳥、不從舊活動價再次折減；不對外列折扣拆解或截止日。'
                        '5人優惠檔位未確認，不猜相鄰檔；7人以上仍需顧問安排。'
                        '已發且仍有效的舊拼團低價承諾保留，不疊加折扣。',source_ref=SOURCE)
    # The catalog compares versions lexically. Use one dated release version
    # for shipped defaults and runtime packages; runtime wins equal versions.
    package['package_version'] = '2026-10-25.party-quotes-v1'
    return package
