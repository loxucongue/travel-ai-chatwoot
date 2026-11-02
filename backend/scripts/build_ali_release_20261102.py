"""Build the reviewed Ali package and media manifest from archived business sources."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'data/knowledge/china2go/route-releases/20261102-ali'
SOURCE = 'https://china2go.com/0808-2/ (2026-10-26 business version)'
ROUTE = 'tibet_ali_15d'


def build():
    archived = json.loads((ROOT/'data/knowledge/china2go/drafts/business-feedback-20261027/manifest.json').read_text(encoding='utf8'))
    keys = ['guge', 'wuma', 'wildlife', 'namtso', 'earth_tree', 'rongbuk', 'vehicle', 'oxygen',
            'guest_review', 'map', 'manasarovar', 'prayer_flags', 'kailash', 'trekkers',
            'followup_kora', 'followup_murals', 'followup_stars', 'followup_yaks']
    names = ['古格王朝', '物瑪措湖中公路', '羌塘野生動物', '納木措', '大地之樹', '絨布旅館房間',
             '9座VIP車座位', '車載供氧設備', '客人轉山回饋', '阿里北線15日行程地圖', '瑪旁雍措',
             '客人掛經幡', '岡仁波齊北壁', '客人轉山徒步', '轉山經幡', '古格壁畫', '藏北星空', '湖邊牦牛']
    assets = []
    for key, name, entry in zip(keys, names, archived['archives'][0]['files'], strict=True):
        assets.append({'asset_key':f'ali15_{key}', 'display_name':name, 'source_archive':archived['archives'][0]['filename'],
            'source_filename':entry['filename'].split('/')[-1], 'sha256':entry['sha256'], 'byte_size':entry['bytes'],
            'route_variant':ROUTE, 'route_variants':[ROUTE], 'content_group_key':'itinerary_overview' if key == 'map' else key,
            'what_it_shows':name, 'source_ref':SOURCE})
    ordinary = ['peach_11d_2027','peach_9d_2027','tibet_essence_8d','tibet_overview_10d','tibet_linzhi_8d','tibet_winter_8d']
    group_keys = ['everest_sunset','everest_guests','basum','peach_castle','ali_wuma','winter_ice','guests_png','guests']
    group_routes = [ordinary[2:4],ordinary[2:4],['tibet_linzhi_8d'],ordinary[:2],[ROUTE],['tibet_winter_8d'],ordinary+[ROUTE],ordinary+[ROUTE]]
    for key, routes, entry in zip(group_keys, group_routes, archived['archives'][1]['files'], strict=True):
        name = entry['filename'].split('/')[-1]
        assets.append({'asset_key':f'private_{key}', 'display_name':name.rsplit('.',1)[0], 'source_archive':archived['archives'][1]['filename'],
            'source_filename':name, 'sha256':entry['sha256'], 'byte_size':entry['bytes'], 'route_variant':routes[0],
            'route_variants':routes, 'content_group_key':'private_guests' if key.startswith('guests') else 'private_highlight',
            'what_it_shows':name.rsplit('.',1)[0], 'source_ref':'https://china2go.com/8425-2/ (2026-10-27 business version)'})
    facts = []
    groups = {}
    def fact(key,text):
        facts.append({'id':f'{ROUTE}.{key}','text':text,'source_ref':SOURCE})
        return f'{ROUTE}.{key}'
    def group(key,text,images=(),initial=True):
        ref=fact(key,text)
        groups[key]={'approved_text':text,'asset_keys':[f'ali15_{k}' for k in images], 'evidence_refs':[ref],
                     'initial_delivery':initial,'delivery_mode':'text_then_assets' if images else 'text_only'}
    group('itinerary_overview','這條是岡仁波齊精華轉山＋阿里大環線北線15天：拉薩出發，經珠峰、岡仁波齊、古格，再走藏北回拉薩。先給您看行程圖～',['map'])
    group('holy_lakes','一路走訪羊卓雍措、瑪旁雍措和納木措三大聖湖。2027羊年轉湖是當地的信仰文化；這條安排車行走訪，不是徒步繞完三座湖。',['manasarovar'])
    group('kora','轉山安排兩天精華徒步，前後有接駁車：第一天約13公里到止熱寺；第二天約11公里，翻越海拔5,630米的卓瑪拉山口後接回塔欽。仍需要體能準備，不能把有接駁車理解成輕鬆無風險。',['kailash'])
    group('prayer_flags','經幡廣場是轉山起點，抬頭能看到神山。這是我們客人掛經幡祈福的照片～',['prayer_flags'])
    group('trekkers','導遊會照顧團員的行進狀況；每個人的體能不同，途中身體不適應及時告知。需要背夫或馬匹可由顧問協助了解，費用另計，也不能保證全段可騎馬。',['trekkers'])
    group('guge','轉山之後會去札達土林、古格王朝遺址，看高原上的古城和土林地貌。',['guge'])
    group('wuma','阿里大北線會走物瑪措、羌塘和當惹雍措，物瑪措的湖中公路也是這段很特別的風景。',['wuma'])
    group('wildlife','穿越羌塘時有機會看到藏野驢、藏羚羊和其他野生動物；是否遇到要看當天情況。',['wildlife'])
    group('earth_tree','大地之樹是河流在荒原留下的紋理，接著經色林措、藏北，再走納木措回拉薩。',['earth_tree','namtso'])
    group('accommodation','行程安排供氧酒店，日喀則升級一晚希爾頓；珠峰安排有獨立衛浴與供氧的絨布旅館，房源須確認。轉山D7住止熱寺多人間，這一晚沒有供氧，並非全程單間或全程供氧。',['rongbuk'])
    group('vehicle','拼團4人成團、最多6人，安排9座VIP制氧車和司機、導遊；車上有供氧設備。獨立包團的車型由顧問依實際人數核對。',['vehicle','oxygen'])
    group('guests','這是客人轉山後的回饋。高原徒步每個人速度不同，行程仍要依身體狀況、天氣及當地安排調整。',['guest_review'])
    group('no_shopping','我們接待港澳台及海外旅客，純玩無購物，無購物約定及違規賠償人民幣5,000元會寫在合約裡。')
    source=(ROOT/'data/knowledge/china2go/drafts/business-feedback-20261027/0808-2.txt').read_text(encoding='utf8')
    itinerary=source.split('每日行程・住宿\n',1)[1].split('費用包含\n',1)[0]
    fact('daily_itinerary',itinerary)
    fact('inclusions','包含：入藏函、旅行證、邊防證與軍區件辦理、門票與觀光車、布達拉宮預約、兩段轉山接駁車、9座制氧車、司機、中文導遊、拉薩接送機或火車、酒店早餐、住宿（通常兩人一房，D7止熱寺多人間）、旅行社責任險、車輛座位險、高原保障險。詳細承保範圍以保單條款為準。')
    fact('exclusions','不含往返拉薩機票或火車票、午晚餐、小費（建議每人每天人民幣60元）、轉山馬匹、背夫、行李托運、單人房差、個人消費與醫療費。入藏相關辦理列包含，個人證件費與具體費用歸屬由顧問依合約核對，不說所有證件費都包含或都不含。')
    fact('rooming','一人可報名且可申請拼房；未拼成仍須補房差，不能保證配房。5/10、6/10房差5,100元，7/10至10/10房差6,000元；D7止熱寺是無供氧多人間，不保證加房差能改單間。')
    fact('fitness','兩天徒步共約24公里、最高5,630米。詢問平時爬山或健走習慣、同行是否有65歲以上長輩；個人高原適宜性需醫師評估。沒有運動習慣可請顧問評估，不承諾騎馬或氧氣可保證安全。65歲以上台胞證旅客依公司報名流程核對健康資料；75歲以上交顧問確認證件及可接待安排，不把原稿敘述說成任何國籍一律不能辦入藏函的法律結論。')
    fact('seasons','2027已公布5/10、6/10、7/10、8/10、9/10、10/10。五月初可能下雪封山，5/10先確認至少4位；1–3位建議了解6/10，但不能未經同意改客人日期或保證成團。10/10先說可能下雪封山、確認是否仍選這天再報價，可提出9/10供選擇。未知名額仍需顧問確認。')
    fact('equipment','可介紹10–20L登山背包、保暖防風衣物、羽絨外套、登山杖、墨鏡、防曬、保溫瓶及高熱量零食。完整裝備清單檔案尚未作為附件提供，只能請顧問補充，不能宣稱已發檔或編下載連結。')
    fact('alternatives','12天、不轉山版及13天中線完整接待腳本尚未發布。去過西藏、只想轉山、只走阿里、改天數或自選日期等訂製需求，了解天數、人數與去過哪些地方後請阿里顧問規劃；不套15天拼團價、不虛構中線住宿。只有客人同意才轉入普通全覽10天，單說納木措或扎西半島先澄清方向。')
    followups=[('kora','藏族文化裡，轉山是一段很有意義的旅程。分享一張轉山路上的畫面～','followup_kora'),
       ('read','前面阿里的行程，您有大概看一下嗎？哪一段最想了解呢？',None),
       ('murals','古格王朝遺址保留的壁畫，是這段行程很特別的歷史風景。','followup_murals'),
       ('stars','分享一張藏北星空～偏遠地區光害少，實際觀星還是要看當晚天氣。','followup_stars'),
       ('yaks','湖泊、牦牛和雪山一起出現在眼前，是阿里很有代表性的風景。','followup_yaks'),
       ('equipment','第一次轉山，衣物、背包和體能準備都可以提早了解；有想問的準備細節，隨時和我們說。',None),
       ('alternative','如果這次時間或體力還在考慮，也可以先了解西藏全覽10天。您有興趣看看嗎？',None)]
    for key,text,photo in followups:group('followup_'+key,text,[photo] if photo else [],False)
    scripts=[];departures=[]
    for month in range(5,11):
        price,room=(24800,5100) if month<7 else (26800,6000)
        sid=f'price_{month:02d}';day=f'2027-{month:02d}-10'
        scripts.append({'id':sid,'name':f'{month}月阿里15天報價','answer_text':f'【岡仁波齊精華轉山＋阿里大環線北線15天】\n出發日：2027/{month}/10\n4–6人精緻小團：人民幣¥{price:,}/人（2人一間；轉山當晚為多人房）\n包含：入藏相關文件、9座制氧車、司機、導遊、保險、住宿、門票、轉山接駁車、酒店早餐\n不含：機票、午晚餐、小費、轉山馬匹與背夫、單人房差及個人花費',
            'asset_ids':[],'topics':['price'],'status':'active','source_ref':SOURCE,'content_group_key':'itinerary_overview',
            'fact_ids':[],'positive_examples':['客人詢問此團期費用且工具已核對'],'negative_examples':['日期未確定','包團詢價'],
            'usage_note':'只有團期工具quoted才引用，不加普通西藏多人折扣或早鳥。單人補充未拼成房差條件；不用一次報所有月份。'})
        departures.append({'date':day,'price_cny':price,'single_supplement_cny':room,'script_id':sid,
            'minimum_party_size':4 if month==5 else 1,'advisory':'可能下雪封山，先確認客人仍選這個日期再報價。' if month==10 else ''})
    package={'schema_version':1,'route_variant':ROUTE,'series':'ali','package_version':'20261102-ali-v1','knowledge_version':'',
       'branch':'西藏阿里','name':'岡仁波齊精華轉山＋阿里大環線北線15天','selection_title':'阿里轉山北線15天',
       'selection_aliases':['阿里大環線＋轉山15天','阿里15天','阿里北線','阿里大北線','轉山','岡仁波齊','羊年轉湖','三大聖湖','古格王朝','物瑪措','羌塘','當惹雍措'],
       'match_keywords':[],'default_entry_message':'岡仁波齊精華轉山＋阿里大環線北線15天','required_slots':['party_size','departure_window'],
       'initial_delivery_interval_seconds':2,'intake':{'enabled':True,'field':'party_size','question':'您好～想先了解一下，這次預計幾位一起去呢？','options':['自己一人','2–3人','4–6人','7人以上'],'wait_seconds':30},
       'ai_guidance':'獨立阿里系列，4人成團最多6人，僅公布2027每月10日的六個團期；不能套普通線價格或2人成團。先問人數，介紹後詢問體能與65歲以上長輩，再問月份。詳讀本線Skill。',
       'routing_guidance':'客戶主動提阿里、轉山、岡仁波齊、轉湖、古格或藏北深度地名，理解為阿里系列需求並讀本線Skill；僅普通珠峰/拉薩不切阿里。單提納木措/扎西半島先問普通西藏還是阿里。不主動向只說12天以上的客人推阿里；中線、13天、不轉山及訂製請顧問核對，不等於選定15天。',
       'knowledge_facts':facts,'content_groups':groups,'content_sequence':[k for k,g in groups.items() if g['initial_delivery']],
       'fixed_answers':scripts,'seasonal_pricing':{'schedule_mode':'fixed_dates','years':[2027],'minimum_notice_days':30,'departures':departures},
       'followup_policy':{'enabled':True,'lead_priority_override':True,'checkpoints':{'fitness_wait':5,'fitness_read_check':30,'fitness_unread_wait':60,'fitness_next_day':1440,'month_wait':1440,'quote_wait':180,'contact_wait':720,'daily':1440},
          'daily_sequence':['followup_'+x[0] for x in followups],'guidance':'模型按對話選檢查點。體能問題依5分鐘、再30分鐘、已讀才繼續；無已讀證據不假設已讀，再等60分鐘/次日。報價後3小時再輕邀；聯系邀請後半天未回開始每天一則，最多七則且仍受渠道窗口與停用保護。未回不等於同意，不重複催聯絡方式；已留資或顧問接手停止洗單。'}}
    package['knowledge_version']=json.loads((ROOT/'data/knowledge/china2go/route-packages/peach-11d-2027/route-package.json').read_text(encoding='utf8'))['knowledge_version']
    DEST.mkdir(parents=True,exist_ok=True)
    for name,value in [('asset-manifest.json',assets),('route-package.json',package)]:
        (DEST/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'assets':len(assets),'groups':len(groups),'facts':len(facts),'departures':len(departures)}))

if __name__ == '__main__':build()
