"""Build reviewable, inactive release packages from the supplied ZIPs and saved page.

No database, model endpoint or customer channel is accessed by this command.
Run from the repository root after saving the authenticated source page.
"""
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / 'data/knowledge/china2go'
DRAFT = KNOWLEDGE / 'route-drafts/20261014'
RELEASE = KNOWLEDGE / 'route-releases/20261014'
SOURCE = KNOWLEDGE / 'website-1013-20261014'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def main():
    sys.path.insert(0, str(ROOT/'backend'))
    from scripts.configure_quote_scope_20261015 import update_package as update_quote_scope
    from scripts.configure_quote_availability_20261015 import update_package as update_quote_availability
    from scripts.configure_private_departure_20261015 import update_package as update_private_departure
    html = (ROOT / 'output/new-routes-20261014/page.html').read_text(encoding='utf8')
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('.elementor-widget-theme-post-content')
    assert article and '西藏常規線' in article.get_text()
    for element in article.select('script, style'):
        element.decompose()
    for image in article.select('img'):
        image.replace_with(image.get('alt', ''))
    source_text = article.get_text('\n', strip=True)
    SOURCE.mkdir(parents=True, exist_ok=True)
    (SOURCE / 'content.txt').write_text(source_text, encoding='utf8')
    (SOURCE / 'content.html').write_text(str(article), encoding='utf8')
    digest = hashlib.sha256((SOURCE / 'content.html').read_bytes()).hexdigest()
    write(SOURCE / 'manifest.json', {'url': 'https://china2go.com/1013-2/', 'captured_at': '2026-10-14',
        'method': 'authenticated_browser', 'raw_dom_sha256': hashlib.sha256(html.encode()).hexdigest(),
        'snapshot_sha256': digest, 'images': 'Original user ZIPs; embedded webpage thumbnails omitted'})
    steps = {s.select_one('.k').get_text(strip=True): s.get_text('\n', strip=True).split('\n', 1)[1]
             for s in article.select('.step') if s.select_one('.k') and '\n' in s.get_text('\n', strip=True)}
    assets = json.loads((DRAFT / 'asset-manifest.json').read_text(encoding='utf8'))
    for asset in assets:
        asset.update(review_state='verified_source_and_zip', available=False)
    for folder, days, prefix, weekday in [('tibet-essence-8d', 8, 'B', '四'), ('tibet-overview-10d', 10, 'A', '二')]:
        package = json.loads((DRAFT / folder / 'route-package.json').read_text(encoding='utf8'))
        rid = package['route_variant']
        ref = 'website-1013-20261014/content.txt'
        package.update(package_version='2026-10-14.release-1',
            publication={'status': 'prepared', 'activation_allowed': True, 'production_enabled': False},
            intake={'field': 'departure_window', 'question': '請問您大概想幾月出發呢？',
                    'options': ['12-2月', '3-4月', '5-6月', '7-8月', '9-11月'], 'wait_seconds': 30})
        package['source'].update(status='verified_authenticated_browser', snapshot_sha256=digest,
            branch_content='data/knowledge/china2go/website-1013-20261014/content.txt')
        package['selection_aliases'] += [f'西藏{days}日遊', f'西藏{days}天遊',
            '西藏全覽10天（林芝+拉薩+珠峰）' if days == 10 else '西藏精華8天（拉薩+珠峰）']
        package['match_keywords'] = deepcopy(package['selection_aliases'])
        package['ai_guidance'] = ('2026-10-14新增常規線，網站2027報價。先問月份，30秒未答就介紹，介紹後才問人數；'
            '已知月份直接介紹。按本線季節表報價，必須用get_departure_quote核算日期，未知日期先問，'
            '不借桃花活動價或30天活動截止日。3-4月建議對應桃花線，須尊重客戶是否要珠峰及天數，'
            '客戶堅持常規8日則按本線有效日期；10日3/11-4/30不售。冬遊未啟用，不能套常規線。'
            '季節性未開放先推薦同時段已啟用且匹配需求的產品；沒有合適選擇，可輕邀聯絡方式登記下次開放通知，僅邀請不轉人工。'
            '7人以上、包團、不到30天出發、只能長假需特殊報價時靜默轉人工。'
            '1-3人介紹後了解同行與長輩孩童；4-6人先了解長輩孩童，有意願再邀請LINE QR Code或微信。'
            '沿用已確認全局沉默節奏與靜默交接規則；不因已讀就當作客戶回答或同意留資。'
            '報價後接輕鬆問句；不反覆索取聯絡方式，不說請顧問或假稱檔案太大／平台漏訊息。'
            '頁面待確認的氧氣濃度、供氧時數與移動氧艙不對客宣稱。')
        facts = [package['knowledge_facts'][0], package['knowledge_facts'][2]]
        def fact(key, text):
            facts.append({'id': rid + '.' + key, 'text': text, 'source_ref': ref})
        fact('itinerary', '拉薩安排布達拉宮、大昭寺、八廓街、扎基寺和色拉寺；羊卓雍措、卡若拉冰川、白居寺、珠峰大本營、絨布寺（依政策）、薩迦寺、扎什倫布寺及拉日鐵路。' +
             ('林芝另安排魯朗林海、秀巴古堡及巴松措。' if days == 10 else '') +
             'ZIP與網頁的第2/3天順序或林芝酒店分店存在差異，具體先後/分店尚待確認，不承諾不一致細節。')
        fact('included', '包含入藏函等旅行證件辦理、門票及觀光車、回程拉日鐵路票、車、司機、中文導遊、接送、住宿及酒店早餐（珠峰簡易早）、旅行社責任險/車輛座位險/高原保障險50萬人民幣；醫用氧氣鋼瓶4L、葡萄糖、每人一罐便攜氧氣。')
        fact('excluded', '不含往返西藏機票或火車票、午晚餐、小費、單房差、個人花費及行程外項目。機票可提供航班建議和協助代訂。司機與導遊小費建議每人每天各人民幣30元，合計60元。此標準只適用本常規線，不能借桃花線的小費標準。')
        fact('group', '4人成團，拼團最多6人，9座VIP車，2人一間；4人独立成团原價每人加400，6人独团原價；未確認優惠不自行相減。7人以上或包團交顧問。')
        fact('hotel', '希爾頓系列供氧酒店，珠峰為供氧絨布旅館，獨立衛浴。頁面將12小時與85-90%氧氣濃度列為待確認，不作保證；林芝分店以確認結果為準。')
        fact('booking', '本表是2027年價格，其他年份需確認。入藏函按業務要求至少提前30天辦理；不足30天不報價交顧問。65歲以上按行程要求提供醫師評估/健康報告；75歲以上、5歲以下由顧問確認適合的替代線路，不斷言法規禁止。')
        package['knowledge_facts'] = facts
        groups = {}
        def group(key, text, suffixes=(), initial=True, evidence='itinerary'):
            groups[key] = {'approved_text': text, 'asset_keys': [rid+'_'+s for s in suffixes],
                           'evidence_refs': [rid+'.'+evidence], 'initial_delivery': initial,
                           'delivery_mode': 'text_then_assets', 'purpose': key}
        def step(label):
            return steps[f'文{prefix}-{label}']
        group('itinerary_overview', f'先給您看一下我們的{days}日' + ('全覽' if days == 10 else '精華') + '西藏行程～', ['itinerary'])
        if days == 10:
            group('nyingchi', '這條從林芝進藏，會去巴松措、秀巴古堡與魯朗林海，給您看看景點照片～', ['basum','castle'])
        group('lhasa', '拉薩會安排布達拉宮、大昭寺與八廓街，也到扎基寺和色拉寺，看看當地的人文風景～', ['potala','zaki','sera'])
        if days == 8:
            group('everest', '珠峰大本營也是這條行程的重點，這是團友拍到的日照金山，能否看到要配合當天天氣喔～', ['everest'])
            group('rongbuk', '珠峰那晚安排供氧絨布旅館，有獨立衛浴喔～', ['rongbuk'], evidence='hotel')
        group('hotel_reference', '其他晚數安排希爾頓系列供氧酒店，給您看看住宿照片～', ['hotel_1','hotel_2'], evidence='hotel')
        group('vehicle_reference', '我們的拼團最多6人，安排9座VIP車，給您看看車內座位～', ['vehicle'], evidence='group')
        group('vehicle_oxygen', '隨車配備醫用氧氣鋼瓶、葡萄糖及每人一罐便攜氧氣。', ['vehicle_oxygen'], evidence='included')
        if days == 10:
            group('everest', '珠峰大本營也是這條行程的重點，這是團友拍到的日照金山，能否看到要配合當天天氣喔～', ['everest'])
            group('rongbuk', '珠峰那晚安排供氧絨布旅館，有獨立衛浴喔～', ['rongbuk'], evidence='hotel')
        group('company', '我們 China2Go 國旅環球主要接待港澳台與全球外賓，安排純玩小團。', ['lulang_guests'] if days == 10 else ['guests'], evidence='scope')
        group('no_shopping', '我們把無購物寫在合約上，有購物行為依約賠償人民幣5,000元。', evidence='scope')
        fact('shopping', '業務原話：無購物寫入合約，有購物行為賠償5000元。')
        groups['no_shopping']['evidence_refs'] = [rid+'.shopping']
        group('read_check', '所以我們的拼團一團最多六人，一個人也能報名喔！請問您們大概幾位呢？', initial=False, evidence='group')
        for key, suffix, text in [
            ('followup_sakya', 'sakya', '行程也安排珠峰下山後去薩迦寺，和您分享這裡的建築風景～'),
            ('followup_railway', 'railway', '回程安排拉日鐵路回拉薩，可以從車窗欣賞沿途風景～')]:
            group(key, text, [suffix], initial=False)
        if days == 8:
            group('barkhor', '與您分享拉薩八廓街的街景～', ['barkhor'], initial=False)
        else:
            for i in range(1,4):
                group('guest_followup_'+str(i), '給您分享一下我們的團友照片～', ['guests_'+str(i)], initial=False, evidence='scope')
        group('contact_request', '方便給我您的 LINE QR Code 或微信嗎？我們可以接著協助您確認詳細安排～', initial=False, evidence='scope')
        group('price_reference', '西藏有淡旺季，我先按出發時間幫您核對價格。請問您預計哪一年、幾月出發呢？', initial=False, evidence='booking')
        package['content_groups'] = groups
        package['content_sequence'] = list(groups)
        title = '西藏全覽10天｜林芝＋拉薩＋珠峰' if days == 10 else '西藏精華8天｜拉薩＋珠峰'
        seasons = [('low','淡季','10/15－3/10',10480 if days==10 else 7980,2400 if days==10 else 2000, [['01-01','03-10'],['10-15','12-31']]),
                   ('summer','次次旺季','5/6－6/30',12280 if days==10 else 9480,3000 if days==10 else 2300,[['05-06','06-30']]),
                   ('peak','旺季','7/1－9/24',12880 if days==10 else 9980,3200 if days==10 else 2800,[['07-01','09-24']])]
        if days == 8:
            seasons.insert(1, ('spring','次旺季','3/11－4/20',9480,2200,[['03-11','04-20']]))
        answers=[]; bands=[]
        for key, label, period, price, supplement, ranges in seasons:
            text = (f'【{title}】\n{label}出發：{period}（每週{weekday}出發）\n'
                    f'6人精緻小團：¥{price:,}/人（2人一間）\n'
                    '包含：入藏函、車、司機、導遊、保險、住宿、門票、酒店早餐\n不含：機票、午晚餐、小費\n現在報名還有優惠喔！')
            assert all(line in source_text for line in text.splitlines()), text
            answers.append({'id':'price_'+key,'name':label+'報價','answer_text':text,'source_ref':ref,
                'content_group_key':'price_reference','topics':['price'],'fact_ids':[rid+'.booking'], 'asset_ids':[],
                'positive_examples':[f'已確認{period}內的2027出發日期，且不是長假/缺價日期'],
                'negative_examples':['未知日期','長假','其他年份','出發不足30天','沒有問價'],
                'usage_note':'必須先用 get_departure_quote 核對日期與返回的 script_id，原樣引用；不自行套用其他季節。',
                'status':'active','answer_origin':'website_verbatim'})
            bands.append({'script_id':'price_'+key,'ranges':ranges,'price_cny':price,'single_supplement_cny':supplement})
        package['seasonal_pricing']={'years':[2027], 'minimum_notice_days':30, 'weekday':1 if days==10 else 3,
            'advisor_months':[12,1,2],
            'holiday_ranges':[['02-01','02-16'],['04-25','05-05'],['09-25','10-14']],
            'unavailable_ranges':[['03-11','04-30']] if days==10 else [['04-21','04-24']], 'bands':bands}
        for a_id, title_, text, evidence in [
            ('hotel','住宿', '行程安排希爾頓系列供氧酒店，珠峰那晚住供氧絨布旅館，有獨立衛浴喔～','hotel'),
            ('tips','小費','司機和導遊小費建議每人每天各人民幣30元，一天合計60元。','excluded'),
            ('airfare','機票','團費不含往返西藏的機票或火車票，我們可以提供航班建議與協助代訂；行程內的拉日鐵路票有包含喔～','excluded'),
            ('quote_date','確認日期',groups['price_reference']['approved_text'],'booking')]:
            answers.append({'id':a_id,'name':title_,'answer_text':text,'source_ref':ref,'content_group_key':'price_reference' if a_id=='quote_date' else 'hotel_reference',
                'topics':[a_id],'fact_ids':[rid+'.'+evidence],'asset_ids':[], 'positive_examples':[title_], 'negative_examples':[],
                'status':'active','answer_origin':'operator_approved'})
        package['fixed_answers']=answers
        for asset in assets:
            if asset['route_variant'] != rid: continue
            matching = [k for k,g in groups.items() if asset['asset_key'] in g['asset_keys']]
            assert len(matching)==1, (asset['asset_key'],matching)
            asset.update(content_group_key=matching[0], recommended_caption=groups[matching[0]]['approved_text'])
        write(RELEASE/folder/'route-package.json', update_quote_availability(update_private_departure(update_quote_scope(package))))
    write(RELEASE/'asset-manifest.json', assets)
    write(RELEASE/'release.json', {'status':'prepared_not_deployed', 'source_snapshot_sha256':digest,
        'routes':['tibet_essence_8d','tibet_overview_10d'], 'asset_references':len(assets),
        'decisions':[
            'Original peach packages, pricing and promotions unchanged.',
            'Global silence schedule and silent handoff retained; route-specific month intake is 30 seconds.',
            'Winter route is not enabled; offer verified alternatives or reopening notification, do not activate an incomplete fifth route.',
            'Seasonal closures use alternatives/opt-in retention. Short notice and special holiday quotes still require an advisor.',
            'Map/source discrepancies: 8-day D2/D3 ordering; 10-day Nyingchi Hilton branch. Exact details not promised.',
            'Only supplied 8-day guest photo bound to 8-day route; no borrowing of the three 10-day guest photos.']})
    sys.path.insert(0, str(ROOT/'backend'))
    from scripts.reconcile_tibet_prices_20261014 import main as reconcile_prices
    reconcile_prices()
    print(json.dumps({'routes':2,'assets':len(assets),'seasonal_quotes':7,'production_changed':False}))


if __name__ == '__main__':
    main()
