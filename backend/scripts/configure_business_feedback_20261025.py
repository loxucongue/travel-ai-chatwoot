"""Apply approved October 24 group-size facts; prices and itineraries stay intact.

Pure package migration: callers review the result, validate and publish through
the existing package mechanism. Importing this module never writes live data.
"""
from copy import deepcopy

SOURCE = '麗麗 2026-10-24 11:22、19:18 業務補充'
ROUTES = {'peach_11d_2027', 'peach_9d_2027', 'tibet_essence_8d',
          'tibet_overview_10d', 'tibet_winter_8d', 'tibet_linzhi_8d'}
GROUP_FACT = ('普通拼團升級為滿2人發團、6人滿團，發團機率很高；1位可報名拼團，'
              '不是1位即可發團，也不是2位獨享整台車。這是成團門檻，並非已報名人數、'
              '即時餘位或某日已確認出團。仍須核對本線開放日期。'
              '4位及以上可獨立成團自選日期的既有條件不變，拼團滿2人不等於2位可獨團。')
REST_FACT = ('部分路段車程較長，具體時長依實際路線與路況。拼團行程與景點停留時間'
             '不能由單個客人隨意更改；導遊和司機會沿途找合適地點安排上廁所、休息。'
             '小團人少，導遊容易照顧大家；車輛座椅及供氧按本線已確認設備介紹。'
             '靈活指照顧客人與合適的休息安排，不是隨叫隨停或任意延長景點停留，'
             '也不保證不疲勞或不高反。')


def update_package(original):
    result = deepcopy(original)
    route = result['route_variant']
    if route not in ROUTES:
        return result
    # Only the old shared-tour minimum changes. Private-group price conditions
    # and the advertised 4-6-person price heading have different meanings.
    for fact in result['knowledge_facts']:
        if fact['id'] == route + '.group':
            fact['text'] = fact['text'].replace('4人成團', '滿2人發團').replace('4人成团', '满2人发团')
            if route == 'tibet_winter_8d':
                fact['text'] = fact['text'].replace('4–6人拼團', '滿2人發團、6人滿團的拼團')
        if fact['id'] == 'route.shared.brand_positioning':
            fact['text'] = fact['text'].replace('我們的拚團都是4-6人一團', '我們的拼團升級為滿2人發團、6人滿團')
    for script in result.get('fixed_answers', []):
        if script['id'] in ('small_party', 'group_party'):
            script['answer_text'] = script['answer_text'].replace(
                '我們的拚團都是4-6人小團', '我們的拼團升級為滿2人發團、6人滿團').replace(
                '我們的拚團都是4-6人一團', '我們的拼團升級為滿2人發團、6人滿團')
            if SOURCE not in script.get('source_ref', ''):
                script['source_ref'] = (script.get('source_ref', '') + '; ' + SOURCE).strip('; ')
    for suffix, text in [('departure_group_policy', GROUP_FACT), ('long_drive_rest', REST_FACT)]:
        fid = route + '.' + suffix
        result['knowledge_facts'] = [f for f in result['knowledge_facts'] if f['id'] != fid] + [
            {'id': fid, 'text': text, 'source_ref': SOURCE}]
    if result != original and not result['package_version'].endswith('.business-20261025'):
        result['package_version'] += '.business-20261025'
    return result
