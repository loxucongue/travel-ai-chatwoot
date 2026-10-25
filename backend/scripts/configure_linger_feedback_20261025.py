"""Reviewable Oct 25 content migration; caller controls DB transaction and release.

Uses the existing route package / material library / configured material rules.
No network, model call, cadence change or production write at import time.
"""
from copy import deepcopy

from scripts.configure_followup_materials_20261019 import configure_assets as import_assets

ROUTES = ('peach_9d_2027', 'peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d',
          'tibet_winter_8d', 'tibet_linzhi_8d')
EVEREST = ('peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d')
SOURCE = 'Linger 2026-10-25 15:45–15:52 業務確認與原始圖片'
PREFIX = 'followup_20261025_'
COMPARISONS = (
    ('peach_9d_2027', 'peach_11d_2027',
     '您同時看了桃花9日和11日，我先給您看較完整的11日行程～兩條的主要差別是11日多了珠峰段，增加2天。'),
    ('tibet_essence_8d', 'tibet_overview_10d',
     '您同時看了精華8日和全覽10日，我先給您看較完整的10日行程～兩條都有珠峰；10日還涵蓋林芝，8日則以拉薩進出，重點安排拉薩與珠峰。'),
)
COMMON = ('先承接客人已表達的需求與顧慮，只有主題適合時才選本素材；每輪一組、最多一個自然問句。'
          '已知日期不再問何時出發，已拒絕或不適用的體驗不推。已講過同一賣點或發過圖片不重複，'
          '不重新介紹整套行程、不留資催促。沒有可用圖片時可以只用文字，不借其他題材圖片。')
COPY = {
    'trust': '西藏就該簡單又純粹🌟\n我們保證無購物！違規賠償您人民幣5,000元，寫在合約裡！\n請問您目前西藏計劃如何呀？',
    'food': '跟您分享西藏的特色美食：🥣 氂牛酸奶、🍦 氂牛冰淇淋，奶香濃郁～\n如果去西藏，您會比較想試哪一種呢？',
    'stars': '西藏的雪山與星空真的很震撼✨\n這是我們客人在珠峰大本營附近拍到的星空～實際觀星需視當天天氣而定。\n您有想過什麼時候親自去看看嗎？',
    'golden': '跟您分享一張日照金山，見者好運唷😊\n想問下您打算什麼時候來西藏玩呀？',
}
PHOTOS = {
    'stars': {'key':'tibet-followup-everest-stars-20261025', 'filename':'everest-stars.jpg',
              'hash':'e2ebf522d2f79eda915c0aa7f40b1cd4a6ea3357a9e435a6db19016c99fbe732',
              'routes':EVEREST, 'name':'珠峰附近星空（業務供圖）', 'caption':COPY['stars'],
              'shows':'繁星、銀河與雪山夜景；業務說明為客人在珠峰大本營附近拍攝。',
              'avoid':['不保證當晚可見星空、不承諾夜宿大本營', '不稱今天拍攝', '不向拒絕珠峰的客人推此體驗']},
    'golden': {'key':'tibet-followup-golden-mountains-20261025', 'filename':'golden-mountains.jpg',
               'hash':'682bb776684bf62a28e318fa789a69598c52be815bcb5e894266992ac96cb34e',
               'routes':ROUTES, 'name':'日照金山', 'caption':COPY['golden'],
               'shows':'陽光照亮雪山山脊，前景為山谷；具體山峰及拍攝時間未確認。',
               'avoid':['不指認為珠峰或其他具名山峰', '不稱今日實拍、不保證行程中能看到同樣天氣景色']},
    'trust': {'key':'tibet-followup-potala-garden-20261025', 'filename':'potala-garden.jpg',
              'hash':'ee9ed1dee966d7e6689430dd53479c9b4773b407b65728942b147b9ee90ff013',
              'routes':ROUTES, 'name':'布達拉宮與花園', 'caption':COPY['trust'],
              'shows':'布達拉宮外觀、花園與草坪；作為西藏風景配圖，不是合約或美食照片。',
              'avoid':['不說圖片本身證明合約內容', '不承諾當季花況、不稱今日實拍']},
}


def update_package(original):
    package = deepcopy(original)
    route = package['route_variant']
    for shorter, longer, summary in COMPARISONS:
        if route in (shorter, longer):
            package['entry_comparison'] = {'other_route': longer if route == shorter else shorter,
                                          'preferred_route': longer, 'summary': summary}
            suffix = '.entry-comparison-20261025'
            if not package['package_version'].endswith(suffix):
                package['package_version'] += suffix
    return package


def update_rules(existing):
    """Append missing rules; preserve operator edits and disabled rules on rerun."""
    rules = deepcopy(list(existing))
    known = {r['id'] for r in rules}
    for topic, text in COPY.items():
        key = PREFIX + topic
        if key in known:
            continue
        photo = PHOTOS.get(topic)
        rules.append({'id':key, 'name':{'trust':'純玩合約承諾', 'food':'氂牛美食',
                                     'stars':'珠峰星空', 'golden':'日照金山'}[topic] + ' · 20261025',
                      'enabled':True, 'purpose':'followup',
                      'route_variants':list(photo['routes'] if photo else ROUTES), 'any_labels':[],
                      'scenario':COMMON + ('本圖使用邊界：' + '；'.join(photo['avoid']) if photo else
                                         '僅介紹美食體驗，不承諾費用包含、免費贈送或特定店家。'),
                      'approved_text':text, 'asset_keys':[photo['key']] if photo else []})
    return rules


def configure_assets(db, media_dir, files):
    return import_assets(db, media_dir, files, photos=PHOTOS, prefix=PREFIX, source=SOURCE)
