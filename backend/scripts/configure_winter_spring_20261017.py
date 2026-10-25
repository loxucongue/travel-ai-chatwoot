"""Apply only the October 16 business changes; preserve other operator settings."""
from copy import deepcopy


SOURCE = 'Business feedback 2026-10-16 21:26 and 22:41-22:46'
DEPARTURE_QUESTION = '這條行程是3/20–4/10出發。請問您大概計劃什麼時候出發呢？'
SPECIALS = {
    'tibet_winter_8d': ('2027-02-05', 10980),
    'tibet_essence_8d': ('2027-02-06', 11480),
    'tibet_overview_10d': ('2027-02-10', 12980),
}
SEASON_GUIDANCE = ('2026-10-16最新規則：客人回答月份先核對季節，再開始適用產品介紹；'
    '四月依通用Skill推薦桃花，已說月份不重問。啟用冬遊目錄時使用其獨立資料，不能把冰川線說成珠峰小鎮線。'
    '只有special_departures中指定日期可用已確認春節價，其他日期仍照原規則；'
    '特價不疊加其他優惠，房型差價、獨團及7位以上仍需確認。'
    '春節常規8/10天若到珠峰，冬季絨布關閉，住宿按已確認的山下珠峰小鎮說明，不發絨布客房照片。')


def update_package(package, *, confirmed_specials=False):
    result = deepcopy(package)
    rid = result['route_variant']
    if rid in ('peach_9d_2027', 'peach_11d_2027'):
        for script in result.get('fixed_answers', []):
            if script['id'] == 'departure_question':
                script['answer_text'] = script['answer_text'].replace(
                    '3/20–4/10出發，每週五、六、日、一發團。', '3/20–4/10出發。')
                note = '客人已回答月份時不再引用；不重述完整團期或逐一列日期，按已介紹狀態自然接續。'
                if note not in script.get('usage_note', ''):
                    script['usage_note'] = (script.get('usage_note', '')+'\n'+note).strip()
    if rid in SPECIALS:
        guidance = result.get('ai_guidance', '')
        guidance = guidance.replace('已知月份直接介紹。', '已知月份先核對季節再介紹。')
        if SEASON_GUIDANCE not in guidance:
            result['ai_guidance'] = guidance+'\n'+SEASON_GUIDANCE
        if confirmed_specials:
            if rid in ('tibet_essence_8d', 'tibet_overview_10d'):
                result['content_groups']['rongbuk']['seasonal_override'] = {
                    'months': [12, 1, 2],
                    'text': '冬季絨布旅館關閉，珠峰那晚安排住山下的珠峰小鎮。',
                    'undated_text': '珠峰住宿會依出發季節安排；冬季絨布旅館關閉，改住山下的珠峰小鎮。',
                    'source_ref': 'Business feedback 2026-10-15 winter accommodation',
                }
                for fact in result['knowledge_facts']:
                    if fact['id'] == rid+'.hotel':
                        fact['text'] = fact['text'].replace('珠峰為供氧絨布旅館，獨立衛浴。',
                            '非冬季珠峰為供氧絨布旅館、獨立衛浴；12–2月絨布旅館關閉，改住山下珠峰小鎮。小鎮酒店名稱、房型及是否獨立衛浴尚未確認，不推斷與絨布相同或不同；客人追問這些未確認設施時靜默交接核對。')
            day, price = SPECIALS[rid]
            script_id = 'price_cny_'+day.replace('-', '')
            policy = result['seasonal_pricing']
            special = {'date': day, 'price_cny': price, 'script_id': script_id,
                       'group_type': 'shared', 'room_occupancy': 2, 'source_ref': SOURCE}
            policy['special_departures'] = [s for s in policy.get('special_departures', []) if s['date'] != day]+[special]
            script = {'id': script_id, 'name': '春節指定團期報價', 'status': 'active',
                'answer_origin': 'operator_approved', 'source_ref': SOURCE,
                'answer_text': (f'【{result["name"]}】\n春節指定出發：{day.replace("-", "/")}\n'
                    f'拼團價格：人民幣¥{price:,}/人（2人一間）\n'
                    '包含：入藏函、車、司機、導遊、保險、住宿、門票、酒店早餐\n不含：機票、午晚餐、小費'),
                'content_group_key': 'price_reference', 'topics': ['price'], 'fact_ids': [], 'asset_ids': [],
                'positive_examples': [day+'拼團價格'], 'negative_examples': ['其他日期', '獨團', '7人以上', '沒有問價格'],
                'usage_note': 'get_departure_quote返回本script_id才可引用。不套普通星期、活動截止日、房差或疊加優惠。'}
            result['fixed_answers'] = [s for s in result['fixed_answers'] if s['id'] != script_id]+[script]
            fact_id = rid+'.cny_departure'
            result['knowledge_facts'] = [f for f in result['knowledge_facts'] if f['id'] != fact_id]+[
                {'id': fact_id, 'text': f'已確認春節指定拼團：{day}出發，人民幣{price:,}元/人，兩人一間；僅限該日期，不代表庫存。', 'source_ref': SOURCE}]
    if result != package and not result['package_version'].endswith('.winter-spring-20261017'):
        result['package_version'] += '.winter-spring-20261017'
    return result


def update_config(config):
    result = deepcopy(config)
    # Narrow exact phrase replacement also covers a global approved-script override.
    for script in result.get('common_scripts', []):
        if script.get('id') == 'departure_question':
            script['text'] = script.get('text', '').replace(
                '3/20–4/10出發，每週五、六、日、一發團。', '3/20–4/10出發。')
    return result
