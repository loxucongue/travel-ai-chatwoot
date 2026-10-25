"""Apply Linger's 16:00 revision through the existing editable material rules.

The three existing photo identities remain stable, including a recompressed
copy of the same star photo, so previously sent pictures remain deduplicated.
Caller owns the transaction and publication. No import-time writes.
"""
from copy import deepcopy

from scripts.configure_linger_feedback_20261025 import COMMON, PREFIX, ROUTES, PHOTOS as PREVIOUS
from scripts.configure_followup_materials_20261019 import configure_assets as import_assets

SOURCE = 'Linger 2026-10-25 16:00–16:01 重新整理的五組圖文'
COPY = {
    'golden': 'Hi～給您分享今日份的日照金山，見者好運唷😊\n\n想問下您打算什麼時候來西藏玩呀？',
    'stars': '西藏有最震撼的雪山、最純淨的星空✨\n\n這是我們客人在珠峰大本營附近拍到的星空～\n\n您有想過什麼時候親自前往西藏，一睹這片風景嗎？🤩',
    'souvenir': '*西藏最夯伴手禮*你知道是什麼嗎？👀💛\n\n答案是——扎基寺小金卡✨\n小小一張，卻被很多人說是“招財小神器”～ 自用送禮都超適合\n\n你會想買幾張帶回來嗎～～',
    'trust': '西藏就該簡單又純粹🌟\n「我們保證無購物！違規賠償您5000人民幣！寫在合約裡！」\n\n請問您目前西藏計劃如何啊？',
    'food': '今天分享西藏「必吃」美食\n🥣 氂牛酸奶\n🍦 氂牛冰淇淋\n\n奶味真的很香！\n很多貴賓第一次吃都說很好吃 😋\n\n如果是你去西藏\n最想試試哪一種呢？ 🫣\n\n另外想問一下～\n我前面傳的訊息您有看到嗎？\n行程上有沒有哪裡不太理解，或想再了解的地方呢？',
}
PHOTOS = deepcopy(PREVIOUS)
PHOTOS.update({
    'souvenir': {
        'key': 'tibet-followup-zaki-souvenir-20261025', 'filename': 'zaki-souvenir.jpg',
        'hash': '84372ab8a5616caccafaaaf401ca156bdfc1e704dbfb236f86f8103023869d58',
        'routes': ROUTES, 'name': '扎基寺小金卡伴手禮',
        'shows': '扎基寺前手持小金卡、護身符及飾品；介紹自願選購的伴手禮。',
        'avoid': ['不承諾招財、轉運等實際功效；招財是民俗寓意',
                  '不安排購物站、不推銷或催購；自願購買與純玩無購物承諾分開說明',
                  '不承諾免費贈送、團費包含或每條路線都到這間寺廟',
                  '客人拒絕購物或擔心購物團時不選此素材'],
    },
    'food': {
        'key': 'tibet-followup-yak-food-20261025', 'filename': 'yak-food.jpg',
        'hash': 'e729dbe747b43af6773a5d389d1d0f2210d54213769d63cd05a76a7c55bf331e',
        'routes': ROUTES, 'name': '氂牛酸奶與冰淇淋',
        'shows': '桌上有酸奶、冰淇淋及一塊白色甜點；業務提供的西藏美食照片。',
        'avoid': ['不臆測店名、價格或第三款甜點名稱', '不承諾團費包含或免費贈送',
                  '客人已說不吃乳製品或不感興趣時不選此素材'],
    },
})
for topic, photo in PHOTOS.items():
    photo['caption'] = COPY[topic]


def update_rules(existing):
    """Revise the approved content; retain enable switches and label targeting."""
    rules = deepcopy(list(existing))
    index = {r['id']: r for r in rules}
    for topic, text in COPY.items():
        key, photo = PREFIX + topic, PHOTOS[topic]
        rule = index.get(key)
        if rule is None:
            rule = {'id': key, 'name': photo['name'] + ' · 20261025', 'enabled': True,
                    'purpose': 'followup', 'route_variants': list(photo['routes']), 'any_labels': []}
            rules.append(rule)
        # Preserve operator-selected audience, including deliberately empty routes.
        assert set(rule['route_variants']) <= set(photo['routes']), 'material_route_scope_conflict'
        rule.update(approved_text=text, asset_keys=[photo['key']],
                    scenario=COMMON + '以下是核准文案；依上下文選擇其中一個問題，不連問，已知日期就省略日期問題。'
                    '今日份指今天分享，不是今天拍攝。' + '使用邊界：' + '；'.join(photo['avoid']))
    return rules


def configure_assets(db, media_dir, files):
    return import_assets(db, media_dir, files, photos=PHOTOS, prefix=PREFIX, source=SOURCE)
