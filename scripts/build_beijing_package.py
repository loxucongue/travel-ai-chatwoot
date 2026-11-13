"""Build the Beijing release from the saved business page; never edit originals."""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing'
ROUTE = 'beijing_micro_5d'
SOURCE = 'https://china2go.com/33-3/'


def build(source_dir):
    raw = (source_dir / 'website-local.txt').read_text(encoding='utf8')
    text = raw[raw.index('China2Go AI對話流程・北京微包團5天'):]
    text = text[:text.rindex('\n推薦行程')].strip()
    RELEASE.mkdir(parents=True, exist_ok=True)
    (RELEASE / 'source.txt').write_text(text, encoding='utf8')
    facts, groups, answers, manifest = [], {}, [], []

    def fact(key, value):
        ref = ROUTE + '.' + key
        facts.append({'id': ref, 'text': value.strip(), 'source_ref': SOURCE + '#' + key})
        return ref

    def group(key, value, assets=(), initial=False):
        ref = fact(key, value)
        groups[key] = {'approved_text': value.strip(), 'asset_keys': list(assets),
                       'evidence_refs': [ref], 'initial_delivery': initial,
                       'delivery_mode': 'text_then_assets', 'purpose': key}

    assets = json.loads((source_dir / 'assets.json').read_text(encoding='utf8'))
    for i, asset in enumerate(assets, 1):
        source = Path(asset['path'])
        key = f'beijing_20261113_{i:02}'
        relative = f'assets/{key}{source.suffix.lower()}'
        target = RELEASE / relative
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        manifest.append({'asset_key': key, 'source_filename': source.name,
                         'display_name': source.stem, 'path': relative,
                         'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                         'byte_size': source.stat().st_size, 'route_variants': [ROUTE]})

    def images(prefix):
        return [a['asset_key'] for a in manifest if a['source_filename'].startswith(prefix)]

    intro = text[text.index('R-1（已知人數）'):text.index('問出發時間 → 報價 → 要聯絡方式', text.index('R-1（已知人數）'))]
    for n in range(1, 10):
        marker = 'R-1（客人沒回人數）' if n == 1 else f'R-{n}\n'
        section = intro.split(marker, 1)[1]
        quote = section[section.index('「') + 1:section.index('」')]
        group(f'intro_{n}', quote, images(f'圖BJ-{n}・'), True)

    # Preserve the full business sections, including the complete DIY table.
    spans = [
        ('intake', '全域規則', 'R-1（已知人數）'),
        ('seasons', '3-1\n推薦月份', '3-2\n'),
        ('toboggan', '3-2\n', '客人有帶小朋友'),
        ('family', '客人有帶小朋友', '客人深度聊歷史'),
        ('history', '客人深度聊歷史', '洗對話（客人沒回，一天發一則）'),
    ]
    for key, begin, end in spans:
        start = text.index(begin)
        value = text[start:text.index(end, start + len(begin))]
        fact(key, value)

    faq = text[text.index('Q1：'):text.index('圖片清單（共 35 張）')]
    for block in re.split(r'(?=Q\d+：)', faq):
        if not block.strip():
            continue
        number = re.match(r'Q(\d+)', block)[1]
        if number == '7':
            block += '\n業務確認：上述小費是每位客人計算。導遊兼司機每位每天人民幣30元，5天150元；導遊與司機分開時每位每天60元。'
        fact('faq_' + number, block)

    for item in manifest:
        group('asset_' + item['asset_key'], item['display_name'], [item['asset_key']])
        item['content_group_key'] = 'itinerary_overview' if item['source_filename'].startswith('圖BJ-1・') else 'asset_' + item['asset_key']

    follow = text[text.index('洗對話-1'):text.index('Q1：')]
    for n in range(1, 7):
        start = follow.index(f'洗對話-{n}\n')
        end = follow.find(f'洗對話-{n+1}\n', start) if n < 6 else len(follow)
        value = follow[start:end]
        quotes = re.findall('「(.*?)」', value, re.S)
        group(f'followup_{n}', '\n\n'.join(quotes) or value.strip(), images(f'圖洗BJ-{n}'))

    # Materialized exact final prices: no model arithmetic, no inherited Tibet discount.
    for season, months, base, supplement in [('low', '1～6月、9～12月', 5780, 1700), ('high', '7～8月', 6280, 1950)]:
        for lo, hi, discount in [(2, 3, 0), (4, 5, 200), (6, 6, 400)]:
            key = f'price_{season}_{lo}_{hi}'
            amount = base - discount
            value = (f'【北京VIP微包團精華5天】\n{lo}'+(f'～{hi}' if lo != hi else '')+
                     f'位成人・{months}出發（春節、五一、十一另計）\n每人：人民幣¥{amount:,}（2人一間）\n'
                     f'單人房差：¥{supplement:,}\n\n'
                     '包含：持證導遊兼司機、VIP專屬座車、4晚四鑽酒店及早餐、行程門票、長城纜車或索道＋滑道二選一、珍寶館、宮事頤庭下午茶、胡同三輪車、24小時接送機、旅遊保險、每日一瓶水。\n'
                     '不含：機票、台胞證、午晚餐、小費（每位每天¥30）、個人消費。\n現在報名還有優惠喔！')
            group(key, value)
            answers.append({'id': key, 'name': f'{months} {lo}～{hi}位優惠價', 'answer_text': value,
                            'source_ref': SOURCE, 'content_group_key': key, 'status': 'active',
                            'topics': ['price'], 'fact_ids': [ROUTE + '.' + key], 'asset_ids': [],
                            'party_size_min': lo, 'party_size_max': hi,
                            'positive_examples': [f'{months}，{lo}位北京5天價格'],
                            'negative_examples': ['兒童價', '春節', '五一', '十一', '非5天訂製', '2028年'],
                            'usage_note': f'僅全部同行人都是成人時適用，2026、2027年標準5日；{months}，{lo}～{hi}位成人。混合成人兒童的家庭不能依總人數使用本模板，也不主動抽出成人部分報價。已扣人數優惠{discount}，不可再扣或加西藏包團費。跨季分別列出，不自行選最低價。'})
    fact('pricing_policy', '北京2位起獨立成團。全年可討論出發日期，無每週定班限制、無入藏函或前後成都交通日。成人最終每人價：1～6月及9～12月，2～3位5780、4～5位5580、6位5380；7～8月分別6280、6080、5880。房差淡季1700、旺季1950。已含人數優惠，不再減早鳥，不加西藏獨團費。1人及7人以上、春節五一十一、非5日或增加城市另算。價格適用2026及2027（使用者授權預設，非原網頁年份）；節假日具體區間未提供，不推斷整個月份均不適用。兒童費用未提供，不自創免費、不按總人數承諾成人優惠。')
    fact('family_quote_boundary', '重要適用條件：價表沒有給兒童計價或兒童是否計入人數優惠。2大2小不等於4位成人，不能確認成人5580，更不能確認全家每人5580。可答「兩位大人加兩位小朋友，需要依孩子年齡與不佔床安排一起核算，我幫您確認家庭總價。」客人已說3歲、8歲、不佔床就不再追問；沒有資料證明不同年齡算法一定不同，不自行斷言。只在客人明確索取成人參考時，說兩位成人檔參考，不保證家庭總價。單人未問兩人比較不主動報兩人價。7位以上不主動報6人參考價。')
    fact('concise_custom_quote', '單人、7位以上、非5日等客製詢價只需：可以安排＋需按需求核算＋適合的私訊邀請，不再背包含不含或其他人數的價。例：「七位可以安排你們自己一團，會按人數安排合適車型；這個人數要另外核算，我幫您確認。方便留個微信、LINE或WhatsApp嗎？」跨季或多人比較，共同包含不含只說一次，直接text並列有依據的價格，不重複兩套完整模板。')
    fact('conversation_precision', '北京接待補充：客人只改月份或人數，直接答新的每人價與必要差異，不重複全部包含不含。不佔床是需求，不能推導免房費或任何費用減免。月份和人數分開理解：「2027年3月6位」表示2027-03、6位，沒有日期6日，客戶卡日期不可補日。14天內訂製交接也須在profile.beijing_request_kind填urgent；其餘訂製custom、7位以上large。例：現在11月13日，11月19日5人慕田峪一日包車，交接同時更新profile.beijing_request_kind="urgent"，只寫內部原因不等於更新分類。')
    fact('followup_price_style', '首次完整詢價用報價模板；已報過價後只改月份或人數，用簡短text只答新每人價與差額，不重複包含不含、房差、天氣和私訊邀請。例如8月4位6080改11月：「11月4位成人，每位優惠價人民幣5,580元，兩人一房，比8月每位少500元～」。不需要追加問題。保留客人的比較意圖，不逼客人選定。')
    package = {'schema_version': 1, 'package_version': '2026-11-13.beijing-micro-v2',
               'knowledge_version': 'china2go-peach-two-products-20260829-v1',
               'route_variant': ROUTE, 'branch': ROUTE, 'name': '北京VIP微包團精華5天',
               'selection_title': '北京微包團5天', 'selection_aliases': ['北京', '北京微包團', '北京5天', '北京五日', '北京包車', '北京故宮', '北京環球影城', 'Beijing'],
               'routing_guidance': '北京獨立微包團5日及北京包車/訂製入口；故宮、天安門、慕田峪、天壇、頤和園、什剎海、北京環球影城等依上下文理解。泛提親子或烤鴨不是自動選線證據。具體一日包車、天津加行程不等於選定標準五日，不啟動整套介紹。',
               'default_entry_message': '想了解北京微包團5天', 'match_keywords': ['北京', 'Beijing'],
               'required_slots': ['party_size', 'departure_window'], 'initial_delivery_interval_seconds': 2,
               'opening_text_in_introduction': True,
               'intake': {'enabled': True, 'field': 'party_size', 'question': '您好～歡迎詢問北京行程！請問這次預計幾位一起去呢？', 'wait_seconds': 30, 'options': ['1人', '2人', '3～6人', '7人以上']},
               'ai_guidance': '先讀beijing-micro-5d。北京獨立微包團，完全不套西藏拼團、入藏函、交通日、早鳥和團期規則。已有明確一日包車或訂製需求先處理，不播放完整5日介紹。依上下文跳過已知問題，先回答再適度邀請私訊。',
               'source': {'url': SOURCE, 'snapshot_sha256': hashlib.sha256(text.encode()).hexdigest()},
               'profile_labels': {'beijing_request_kind': {'urgent': ['急件'], 'custom': ['北京訂製'],
                                                          'large': ['高價值・北京包團'], 'sensitive': ['敏感・轉真人']},
                                  'reception_status': {'sensitive': ['敏感・轉真人'], 'abusive': ['敏感・轉真人']}},
               'knowledge_facts': facts, 'content_groups': groups,
               'content_sequence': [f'intro_{n}' for n in range(1, 10)], 'fixed_answers': answers,
               'followup_policy': {'enabled': True, 'checkpoints': {'quote_wait': 180, 'daily_wait': 1440},
                                   'daily_sequence': [f'followup_{n}' for n in range(1, 7)],
                                   'allow_repeat_invitation': False}}
    (RELEASE / 'route-package.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf8')
    (RELEASE / 'asset-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.source_dir)
    print(json.dumps({'route': result['route_variant'], 'facts': len(result['knowledge_facts']),
                      'groups': len(result['content_groups']), 'prices': len(result['fixed_answers'])}))
