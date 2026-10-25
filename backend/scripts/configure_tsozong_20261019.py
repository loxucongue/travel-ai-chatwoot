"""Narrow, idempotent application of Lili's October 18 itinerary clarification."""
from copy import deepcopy

SOURCE = '麗麗 2026-10-18 13:59 業務補充（措宗寺）'
ROUTES = {'peach_11d_2027', 'peach_9d_2027', 'tibet_overview_10d', 'tibet_winter_8d'}
ANSWER = '這是巴松錯湖心島上的措宗寺，我們會逛湖心島；只要寺廟有開，導遊都會帶大家逛，通常一年四季只有冬季會較多時間關閉。'
GUIDANCE = ('措宗寺（也常寫錯宗寺、错宗寺）位於巴松錯／巴松措湖心島。'
    '本線含巴松措，會逛湖心島；寺廟開放時導遊會帶大家逛，冬季較多時間關閉。'
    '不能因行程沒有單獨列寺名就否認包含；不能保證某日開門、冬季一定關閉或全年開放。'
    '一般是否參觀問題直接回答，不必轉人工；即時開放時間、法會或其他未確認安排另核實。')


def update_package(package):
    result = deepcopy(package)
    rid = result['route_variant']
    if rid not in ROUTES:
        return result
    fid = rid + '.basum_tsozong'
    result['knowledge_facts'] = [f for f in result['knowledge_facts'] if f['id'] != fid] + [
        {'id': fid, 'text': GUIDANCE, 'source_ref': SOURCE}]
    sid = 'basum_tsozong_temple'
    result['fixed_answers'] = [s for s in result['fixed_answers'] if s['id'] != sid] + [{
        'id': sid, 'name': '巴松錯湖心島措宗寺', 'status': 'active',
        'answer_origin': 'operator_approved', 'source_ref': SOURCE, 'answer_text': ANSWER,
        'content_group_key': 'itinerary' if rid == 'tibet_winter_8d' else 'itinerary_overview',
        'topics': ['itinerary'], 'fact_ids': [fid], 'asset_ids': [],
        'positive_examples': ['措宗寺有嗎？', '錯宗寺會去嗎？', '巴松措湖心島的寺廟有安排嗎？'],
        'negative_examples': ['不含巴松措的線路', '某天確切開放時間', '保證冬天開門'],
        'usage_note': '僅用於本線巴松措安排；回答冬季開放問題時保留開放才參觀的條件。' }]
    if GUIDANCE not in result.get('ai_guidance', ''):
        result['ai_guidance'] = (result.get('ai_guidance', '') + '\n' + GUIDANCE).strip()
    if result != package and not result['package_version'].endswith('.tsozong-20261019'):
        result['package_version'] += '.tsozong-20261019'
    return result
