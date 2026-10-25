"""Narrow, idempotent update of approved winter advice and private-group answer."""
from copy import deepcopy
import json
from pathlib import Path

WINTER = {
    'months': [12, 1, 2], 'name': '季節限定冰川行程',
    'explanation': '這段時間的珠峰絨布旅館處於關閉狀態，需要住到山下的珠峰小鎮；這段時間我們有另外一條季節限定的冰川行程。',
    'source_ref': 'Linger 2026-10-14 21:33 business feedback; recommendation only, dates/prices not published',
}
GUIDANCE = ('2026-10-15最新口徑優先於舊版冬遊未啟用的泛化說明：12–2月先說明絨布旅館關閉、'
            '冬季住山下珠峰小鎮，推薦季節限定冰川行程並問興趣；不能只叫客人等開放或立即索QR Code。'
            '這是已確認的推薦資訊，不等於冬遊可自動報價或發全套介紹；完整資料、團期、價格由內部靜默交接確認。'
            '4位獨立成團加價可用private_group_supplement；單問加價不必先算整團季節價，拼團不套獨團加價。'
            '沉默頻率以當前全局daily_at配置為準，舊版首小時多次/3小時不再適用。')


def update_package(package):
    result = deepcopy(package)
    if result.get('route_variant') not in ('tibet_essence_8d', 'tibet_overview_10d'):
        return result
    result['seasonal_pricing']['winter_recommendation'] = deepcopy(WINTER)
    if GUIDANCE not in result.get('ai_guidance', ''):
        result['ai_guidance'] = result.get('ai_guidance', '') + '\n' + GUIDANCE
    if not any(s['id'] == 'private_group_supplement' for s in result['fixed_answers']):
        result['fixed_answers'].append({
            'id': 'private_group_supplement', 'name': '4位獨立成團加價說明',
            'answer_text': '如果您們4位獨立成團，需在原團費基礎上每人加人民幣400元；這是獨立成團的加價，參加拼團不適用這項加價。您們想自己一團，還是和其他旅客拼團呢？',
            'source_ref': 'Approved group fact + Linger 2026-10-14 21:56',
            'status': 'active', 'answer_origin': 'operator_approved',
            'content_group_key': result['fixed_answers'][0]['content_group_key'],
            'topics': ['private_group_supplement'], 'fact_ids': [], 'asset_ids': [],
            'positive_examples': ['4個人走要不要加錢', '四位獨立成團加多少'],
            'negative_examples': ['我們4人參加拼團', '6位獨立成團'],
            'usage_note': '僅答4位獨立成團的額外費用。上文已推薦自己一團時可條件式承接；明確選獨團就省去末尾二選一。未知拼團/獨團先區分，不斷言所有4人均加價。要總價仍需核日期；未核實優惠不自行相減；已留資或要真人靜默交接。',
        })
    for script in result['fixed_answers']:
        if script['id'] == 'private_group_supplement':
            script['answer_text'] = script['answer_text'].replace(
                '您們比較想自己一團，還是和其他旅客拼團呢？',
                '您們想自己一團，還是和其他旅客拼團呢？')
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-releases/20261014'
    for path in root.glob('*/route-package.json'):
        source = json.loads(path.read_text(encoding='utf8'))
        path.write_text(json.dumps(update_package(source), ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print('Updated two source packages; no live data modified.')
