"""Prepare the Linzhi product for isolated acceptance. No DB, network or deployment."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'data/knowledge/china2go/route-releases/20261020-linzhi8'
ROUTE = 'tibet_linzhi_8d'
SOURCE_REF = 'route-releases/20261020-linzhi8/source/business-page.txt'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def prepare(archive, source, output=RELEASE):
    text = source.read_text(encoding='utf8')
    for required in ('不上珠峰・林芝8天', '¥8,280/人', '¥9,580/人', '¥9,980/人', '文L-24'):
        if required not in text:
            raise ValueError('business_source_incomplete:' + required)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'source').mkdir(exist_ok=True)
    (output / 'source/business-page.txt').write_text(text, encoding='utf8')
    facts, groups, answers, manifest = [], {}, [], []

    def fact(key, value):
        facts.append({'id': ROUTE + '.' + key, 'text': value, 'source_ref': SOURCE_REF})

    fact('scope', '林芝入藏、不上珠峰8日是獨立產品，不是拉薩進出且含珠峰的西藏精華8天，也不是冬遊冰川8天或桃花9日。林芝進、拉薩出。')
    fact('itinerary', 'D1林芝接機；D2色季拉山遠眺南迦巴瓦峰、魯朗林海、扎西崗村及魯朗騎馬射箭；D3秀巴古堡、巴松措湖心島、前往拉薩；D4扎基寺、色拉寺；D5布達拉宮、大昭寺、八廓街、景觀下午茶；D6羊卓雍措、卡若拉冰川、白居寺、日喀則；D7扎什倫布寺、拉日鐵路回拉薩；D8拉薩送機或送火車。')
    fact('hotel', '林芝希爾頓系列、拉薩希爾頓惠庭、日喀則希爾頓，所有酒店有供氧，日喀則升級一晚五星。不上珠峰，不住絨布旅館。不能保證氧氣濃度或供氧時數。')
    fact('altitude', '林芝入藏、先住林芝；不去珠峰但仍經色季拉山和卡若拉冰川，並非全程低海拔。供氧及不上珠峰都不保證不會高反；適合與否需個別評估。卡若拉最高海拔數字在業務頁標為待確認，不報5600米或其他猜測值。不作醫療適宜性判定或用藥指導。')
    fact('group', '拼團4人成團、最多6人，9座VIP車。4位以上可獨立成團自選日期，仍受季節和長假條件限制；4位獨團每人加400元，6位獨團原價，5位加價未給不能推算。7位以上由顧問安排。獨行不等於單住。')
    fact('included', '包含入藏函等旅行文件、行程內門票及觀光車、拉日鐵路票、車、司機、中文導遊、接送、住宿及每日酒店早餐、責任險/座位險及高原保障險50萬人民幣。隨車醫用氧氣鋼瓶4L、葡萄糖、每人一罐便攜氧氣。包含布宮景觀下午茶、旱地拔蔥影片、撒隆達、掛經幡及魯朗騎馬射箭；體驗遇天氣/管制可能調整，藏曆新年騎馬射箭暫停。')
    fact('excluded', '不含往返西藏機票或火車票、午晚餐、小費、單房差、個人消費及行程外項目。司機和導遊小費建議每人每天各人民幣30元，共60元。石鍋雞可請導遊協助安排，不包含在團費內，不承諾特殊餐食一定可安排。')
    fact('booking', '價格表為2027年、人民幣每人、拼團兩人一間。入藏函按業務要求至少提前30天；不足30天由顧問確認。3/11–4/30本線不售；春節、五一、十一另計。冬季入口和報價表矛盾，1/2/12月先推薦已公布冬遊，堅持本線則顧問確認，不宣稱冬季已停賣或直接報本線價。')
    fact('shopping', '純玩無購物，無購物約定寫入合約，違約依約賠償人民幣5000元。')

    # ZIP filenames are provenance, not executable paths. Store under a content hash.
    assets = [
        ('itinerary', '圖L-2・', '林芝8日行程圖', '林芝進、拉薩出，不含珠峰的8日行程圖'),
        ('potala', '圖L-10・', '布達拉宮', '布達拉宮景觀'),
        ('jokhang', '圖L-11・', '大昭寺', '大昭寺景觀'),
        ('tashilhunpo', '圖L-14・', '扎什倫布寺', '日喀則扎什倫布寺'),
        ('railway', '圖L-16・', '拉日鐵路', '拉日鐵路沿途風景'),
        ('hotel', '圖L-18・', '拉薩供氧酒店', '房內圈出的供氧設備；圖片不證明濃度或時數'),
        ('vehicle', '圖L-20・', 'VIP車座位', 'VIP車內座位'),
        ('oxygen', '圖L-21・', '車上供氧設備', '車上供氧設備，不據圖片推斷供氧濃度'),
        ('guests', '圖L-23・', '團友合照', '團友與旗幟合照'),
        ('namcha', '圖L-5・', '南迦巴瓦峰', '南迦巴瓦峰景觀，非珠峰'),
        ('lulang_guests', '圖L-6・', '魯朗體驗合照', '魯朗騎馬體驗合照'),
        ('basum', '圖L-8・', '巴松措', '巴松措風景'),
        ('followup_lulang', '洗對話-1・', '魯朗騎馬射箭', '團友在魯朗的騎馬射箭體驗'),
        ('followup_flags', '洗對話-2・', '團友掛經幡', '團友掛經幡照片；不是保單或保額證明'),
        ('followup_tashilhunpo', '洗對話-5・', '扎什倫布寺風景', '扎什倫布寺風景'),
        ('followup_railway', '洗對話-6・', '鐵路風景', '拉日鐵路風景'),
        ('followup_chicken', '洗對話-7・', '墨脫石鍋雞', '當地餐食；午晚餐自理，不當作贈送餐'),
        ('followup_forest', '第4天・', '魯朗林海', '魯朗林海風景；檔名不作行程日期依據'),
    ]
    with zipfile.ZipFile(archive) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) != len(assets):
            raise ValueError('unexpected_archive_asset_count')
        for key, prefix, title, caption in assets:
            matches = [m for m in members if Path(m.filename).name.startswith(prefix)]
            if len(matches) != 1:
                raise ValueError('missing_or_ambiguous_asset:' + key)
            member = matches[0]
            data = z.read(member)
            digest = hashlib.sha256(data).hexdigest()
            suffix = Path(member.filename).suffix.lower()
            target = output / 'assets' / (digest + suffix)
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            with Image.open(target) as im:
                width, height = im.size
                im.verify()
            manifest.append({'asset_key': ROUTE + '_' + key, 'route_variant': ROUTE,
                'source_archive': archive.name, 'source_filename': Path(member.filename).name,
                'path': target.relative_to(output).as_posix(), 'sha256': digest,
                'byte_size': len(data), 'width': width, 'height': height, 'display_name': title,
                'recommended_caption': caption, 'what_it_shows': caption,
                'review_state': 'source_verified_pending_acceptance', 'available': False})

    def group(key, value, asset_ids=(), initial=True, evidence='itinerary'):
        groups[key] = {'approved_text': value, 'asset_keys': [ROUTE + '_' + a for a in asset_ids],
            'evidence_refs': [ROUTE + '.' + evidence], 'initial_delivery': initial,
            'delivery_mode': 'text_then_assets', 'purpose': key}

    group('itinerary_overview', '先給您看「林芝入藏、不上珠峰8日」的行程圖～從林芝到拉薩、日喀則，最後搭拉日鐵路回拉薩。', ['itinerary'])
    group('altitude', '前兩晚先住林芝，這條不去珠峰；但仍有高海拔景點，不能保證不會高反喔。', evidence='altitude')
    group('lulang', '會在色季拉山遠眺南迦巴瓦峰，再到魯朗林海，也安排騎馬、射響箭體驗～能否看到山峰要配合當天天氣。', ['namcha', 'lulang_guests'])
    group('castle', '也會參觀千年歷史的秀巴古堡，看看林芝的人文風景～')
    group('basum', '巴松措和湖心島也有安排，湖水像綠寶石一樣～', ['basum'])
    group('lhasa', '拉薩安排布達拉宮、大昭寺、八廓街、扎基寺和色拉寺，還有布宮景觀下午茶。', ['potala', 'jokhang'])
    group('shigatse', '接著看羊卓雍措、卡若拉冰川與白居寺，再到日喀則參觀扎什倫布寺。', ['tashilhunpo'])
    group('railway', '最後搭拉日鐵路回拉薩，沿途欣賞河谷與高原風景，也減少一段車程。', ['railway'])
    group('hotel_reference', '所有酒店都有供氧，安排希爾頓系列，日喀則升級一晚五星～這是房內的供氧設備。', ['hotel'], evidence='hotel')
    group('vehicle_reference', '拼團最多6人，安排9座VIP車；隨車有醫用氧氣鋼瓶、葡萄糖及便攜氧氣。', ['vehicle', 'oxygen'], evidence='included')
    group('company', '我們 China2Go 國旅環球接待港澳台與全球外賓，安排純玩小團，無購物約定也寫進合約。', ['guests'], evidence='shopping')
    group('switch_difference', '這條保留林芝、拉薩與日喀則，不安排珠峰和絨布住宿；前兩晚住林芝，最後搭拉日鐵路回拉薩。', initial=False)
    for key, value in [
        ('followup_lulang', '跟您分享團友在魯朗騎馬、射響箭的體驗照～這項體驗有包含在行程裡。'),
        ('followup_flags', '跟您分享團友一起掛經幡的照片～這也是行程安排的在地體驗。'),
        ('followup_forest', '這是魯朗林海的風景，山谷裡有大片森林，給您看看～'),
        ('followup_tashilhunpo', '跟您分享日喀則扎什倫布寺的風景，這條行程也會到訪～'),
        ('followup_railway', '回程的拉日鐵路也是行程的一部分，可以從車窗欣賞沿途風景～'),
        ('followup_chicken', '這是林芝的墨脫石鍋雞～午晚餐自理，有興趣可以請導遊協助安排。'),
    ]:
        group(key, value, [key], initial=False, evidence='excluded' if key.endswith('chicken') else 'included' if key.endswith(('flags','lulang')) else 'itinerary')
    group('quote_date', '西藏有淡旺季，請問您預計哪一年、幾月出發呢？', initial=False, evidence='booking')

    def answer(key, name, value, category, evidence, positive, negative=()):
        answers.append({'id': key, 'name': name, 'answer_text': value, 'source_ref': SOURCE_REF,
            'content_group_key': category, 'topics': [key], 'fact_ids': [ROUTE + '.' + evidence],
            'asset_ids': [], 'positive_examples': list(positive), 'negative_examples': list(negative),
            'status': 'active', 'answer_origin': 'operator_approved'})

    bands = []
    for key, label, period, price, room, ranges in [
        ('low','淡季','10/15－3/10',8280,1900,[['01-01','03-10'],['10-15','12-31']]),
        ('summer','次次旺季','5/6－6/30',9580,2200,[['05-06','06-30']]),
        ('peak','旺季','7/1－9/24',9980,2200,[['07-01','09-24']]),
    ]:
        value = (f'【不上珠峰・林芝8天｜林芝＋拉薩＋日喀則】\n{label}出發：{period}（每週二出發）\n'
                 f'6人精緻小團：¥{price:,}/人（2人一間）\n'
                 '包含：入藏函、車、司機、導遊、保險、住宿、門票、酒店早餐\n'
                 '不含：機票、午晚餐、小費\n現在報名還有優惠喔！')
        if not all(line in text for line in value.splitlines()):
            raise ValueError('quote_not_in_source:' + key)
        answer('price_' + key, label + '報價', value, 'quote_date', 'booking',
               ['本線2027年相應季節，get_departure_quote返回quoted'], ['其他線路', '未問團費', '冬季待確認', '未知日期'])
        answers[-1]['answer_origin'] = 'website_verbatim'
        bands.append({'script_id': 'price_' + key, 'price_cny': price,
                      'single_supplement_cny': room, 'ranges': ranges})
    answer('altitude', '高反疑慮', '這條從林芝入藏、不去珠峰，但仍有高海拔景點；酒店和車上有供氧，也不能保證不會高反。若擔心身體狀況，可以先帶行程請醫師評估喔。', 'altitude', 'altitude', ['會不會高反', '海拔高嗎'])
    answer('tips', '小費', '司機和導遊小費建議每人每天各人民幣30元，一天共60元。', 'quote_date', 'excluded', ['小費多少'])
    answer('airfare', '機票', '團費不含往返西藏的機票或火車票，行程內的拉日鐵路票有包含喔～', 'quote_date', 'excluded', ['含機票嗎'])
    answer('quote_date', '出發時間', groups['quote_date']['approved_text'], 'quote_date', 'booking', ['詢價但未說年份月份'])
    answer('tsozong', '措宗寺', '這是巴松措湖心島上的措宗寺，我們會逛湖心島；只要寺廟有開，導遊都會帶大家逛，通常冬季會有較多時間關閉。', 'basum', 'itinerary', ['措宗寺有去嗎', '錯宗寺有嗎'])

    base = json.loads((ROOT / 'data/knowledge/china2go/route-packages/peach-9d-2027/route-package.json').read_text(encoding='utf8'))
    package = {'schema_version': 1, 'package_version': '2026-10-20.linzhi8-rc1',
        'knowledge_version': base['knowledge_version'], 'route_variant': ROUTE, 'branch': 'tibet_linzhi_8d',
        'name': '不上珠峰・林芝入藏8日', 'selection_title': '不上珠峰・林芝8天',
        'selection_aliases': ['不上珠峰 林芝入藏8日', '不上珠峰林芝入藏8日', '不上珠峰・林芝8天',
            '不上珠峰林芝8日', '林芝入藏8日', '林芝8天', '林芝8日', '林芝希爾頓8日'],
        'default_entry_message': '不上珠峰 林芝入藏8日',
        'match_keywords': ['不上珠峰 林芝入藏8日', '不上珠峰林芝8日', '林芝入藏8日', '林芝8天', '林芝8日'],
        'required_slots': ['departure_window', 'party_size'], 'initial_delivery_interval_seconds': 2,
        'intake': {'field':'departure_window', 'question':'請問您大概想幾月出發呢？',
                   'options':['12-2月','3-4月','5-6月','7-8月','9-11月'], 'wait_seconds':30},
        'publication': {'status':'acceptance_only', 'activation_allowed':False, 'production_enabled':False,
                        'requires_user_approval':True},
        'source': {'url':'https://china2go.com/1-2/', 'modified':'2026-10-20T21:01:43',
                   'snapshot_sha256':hashlib.sha256(text.encode('utf8')).hexdigest(),
                   'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                   'branch_content':SOURCE_REF},
        'ai_guidance': '廣告「不上珠峰 林芝入藏8日」直接識別本線。怕高反或帶長輩是推薦訊號，不等於同意換線；單問高反先回答。明確換本線才route_choice=selected；推薦用recommend和profile.discussion_route，保留原route_variant。明確不上珠峰寫everest_preference=avoid，僅擔心寫altitude_concern=true，不能猜已拒絕珠峰。已知月份人數不重問；中途切線僅發差異介紹，取消舊圖文。三四月按具體日期核對桃花9日，不能把只有8天或4/20等需求硬轉9日。冬季銷售口徑待確認，先推薦冬遊，堅持本線由顧問確認。價格限本線2027工具核價，普通報價不主動提房差。已確認通用接待、人工控制及七天沉默規則優先，不套網頁八天後無限跟隨、反覆索要LINE、假稱平台漏訊息或移動氧艙保證。',
        'knowledge_facts': facts, 'content_groups': groups, 'content_sequence': list(groups),
        'switch_introduction_sequence': ['itinerary_overview', 'switch_difference'],
        'fixed_answers': answers,
        'seasonal_pricing': {'years':[2027], 'minimum_notice_days':30, 'weekday':1,
            'private_departure_min_party_size':4, 'advisor_months':[], 'review_months':[1,2,12],
            'review_reason':'林芝8日冬季入口與價格表銷售口徑不一致，需顧問確認；不代表已停賣，不套冬遊或其他8天價格。',
            'holiday_ranges':[['02-01','02-16'],['04-25','05-05'],['09-25','10-14']],
            'unavailable_ranges':[['03-11','04-30']], 'bands':bands}}
    for asset in manifest:
        matches = [k for k,g in groups.items() if asset['asset_key'] in g['asset_keys']]
        if len(matches) != 1:
            raise ValueError('asset_group_mapping:' + asset['asset_key'])
        asset['content_group_key'] = matches[0]
    write(output / 'tibet-linzhi-8d/route-package.json', package)
    write(output / 'asset-manifest.json', manifest)
    write(output / 'release.json', {'status':'acceptance_only_not_deployed', 'route':ROUTE, 'assets':len(manifest),
        'production_changed':False, 'requires_user_approval':True,
        'review_items':['冬季林芝8日是否持續銷售；目前只交顧問核對，不自動報價。',
                        '具體高海拔數字、移動氧艙性能不採用未確認內容。',
                        '網站八天後持續跟隨改為現行最後客戶回覆起七天。']})
    return deepcopy(package), manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    _, assets = prepare(args.archive, args.source)
    print(json.dumps({'prepared':str(RELEASE), 'assets':len(assets), 'production_changed':False}))
