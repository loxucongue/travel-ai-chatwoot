from pathlib import Path
import json,re,hashlib,shutil
from bs4 import BeautifulSoup
from PIL import Image
src=Path(__file__).parent
out=Path('E:/ai_code/travel_ai_Chatwoot-yunnan-20261125/releases/yunnan-tw-20261125')
out.mkdir(parents=True,exist_ok=True)
live=json.loads((src/'live-before.json').read_text(encoding='utf8'))
route='yunnan_tw_8d';url='https://china2go.com/雲南台灣自己/'
html=next((src/'source').glob('*.html')).read_text(encoding='utf8')
b=BeautifulSoup(html,'html.parser')
texts=(src/'sop.txt').read_text(encoding='utf8')
website=(src/'website.txt').read_text(encoding='utf8')
assert texts[:10000]==website[:10000], 'source mismatch'
guidance='''台灣市場的七彩雲南昆大麗＋瀘沽湖8天。先讀yunnan-tw-8d。絕不套用西藏入藏函、30天、交通日、早鳥、4人成團或北京包團價格。廣告2人成團/最多6人不是客人同行人數。
1人拼團拼房需核實；2–3人每週五現有團期，7座商務車、司機兼導遊；4–6人可另開新團期，9座VIP車、司機＋專職導遊；7人以上獨立訂製，不套標準團價。4–6人開新團期不擅自改記成獨立包團。
先答當輪問題再問一個缺項。已有時間/人數不重問。明確包車、只去麗江或加香格里拉先了解需求，不播放整套標準8日。介紹按R1–R13順序，已介紹不重播。涉及價格先讀price_policy，按月份和實際人數報；無有效年份證據只稱目前參考價，遠期需確認。不自行套用人數折扣。
索道天氣/停開以景區公告為準，不能保證登頂、無高反或醫療安全。7座照片只作7座參考，不能冒充9座實車。旅拍、供氧房、入藏函不是本產品贈送項。
聯繫方式可選LINE/微信/WhatsApp的QR Code或ID；首次不發官方帳號，拒絕個人帳號才提供官方LINE @china2go或微信china2go-cits；拒絕轉平台尊重，不反覆索取；wxid_原始ID請補QR Code。不要聲稱已加好友或檔案必定傳不了。
介紹後考慮/討論不能只說慢慢看：承接後補一個適用缺項或自然邀請。報價後沿用全局5分鐘檢查；覆蓋來源3小時。介紹後未知月份可等待30分鐘，後續一天最多一則新主題；不得重發已展示圖文。六則用完無新內容就等待，不無限循環。人工接管、拒絕、已留資、渠道限制優先；當天再次邀請仍需去重和時段限制。
同一則訊息避免同時用「中國」和「台灣」，目的地稱大陸/雲南。少數民族只談文化生活；政治宗教爭議或攻擊轉人工。公司辦公室/營業執照沿用全局已核准固定答覆與轉人工。'''
p=dict(schema_version=1,package_version='2026-11-25.yunnan-tw-v1',knowledge_version=live['packages']['beijing_micro_5d']['knowledge_version'],route_variant=route,branch=route,name='七彩雲南・昆大麗＋瀘沽湖8天',selection_title='雲南昆大麗瀘沽湖8天',selection_aliases=['雲南','云南','昆大麗','昆大丽','雲南8天','雲南八天','昆明','大理','麗江','丽江','瀘沽湖','泸沽湖','沙溪','喜洲','玉龍雪山','玉龙雪山','洱海','Yunnan'],match_keywords=['雲南','云南','昆大麗','昆大丽','瀘沽湖','泸沽湖','玉龍雪山','玉龙雪山','Yunnan'],routing_guidance='明確詢問雲南/昆大麗/麗江/瀘沽湖可識別本線；單獨提到索道、篝火、雪山不構成換線。指定只去一城、包車或加香格里拉是訂製需求，先澄清，不自動選標準8日。',default_entry_message='想了解雲南昆大麗瀘沽湖8天',required_slots=['party_size','departure_window'],initial_delivery_interval_seconds=3,opening_text_in_introduction=True,intake={'enabled':True,'field':'party_size','question':'您好～歡迎詢問雲南行程！請問這次預計幾位一起去呢？','wait_seconds':30,'options':['1人','2～3人','4～6人','7人以上']},ai_guidance=guidance,source={'url':url,'archive_sha256':hashlib.sha256(html.encode()).hexdigest(),'website_sha256':hashlib.sha256(website.encode()).hexdigest()},knowledge_facts=[],content_groups={},content_sequence=[],fixed_answers=[])
assets={};manifest=[]
for f in sorted((src/'source'/'圖片').glob('*.jpg')):
 token=f.stem.split('_',1)[0];key=route+'_'+re.sub('[^a-z0-9]+','_',token.replace('圖季','season_').replace('圖月','month_').replace('圖洗','follow_').replace('圖食','food_').replace('圖','').replace('保證','guarantee').lower()).strip('_')
 assert token not in assets and key not in assets.values()
 raw=f.read_bytes();sha=hashlib.sha256(raw).hexdigest()
 with Image.open(f) as im:im.verify()
 assets[token]=key;(out/'media').mkdir(exist_ok=True);(out/'media'/f'{sha}.jpg').write_bytes(raw)
 manifest.append(dict(asset_key=key,route_variant=route,source_archive='雲南昆大麗瀘沽湖8天_圖片.zip',source_filename=f.name,sha256=sha,byte_size=len(raw),display_name=f.stem.split('_',1)[1],content_group_key='library',content_family='yunnan',what_it_shows=f.stem.split('_',1)[1],recommended_caption=f.stem.split('_',1)[1],available=True))
def group(key,text,tokens=(),initial=False):
 fid=route+'.'+key
 p['knowledge_facts'].append(dict(id=fid,text=text,source_ref=url+'#'+key))
 p['content_groups'][key]=dict(approved_text=text,asset_keys=[assets[t] for t in tokens],evidence_refs=[fid],initial_delivery=initial,delivery_mode='text_then_assets' if tokens else 'text_only')
 for a in manifest:
  if a['asset_key'] in p['content_groups'][key]['asset_keys'] and a['content_group_key']=='library':a['content_group_key']=key
 return fid
intro={}
for st in b.select('.st'):
 k=st.select_one('.k');say=st.select_one('.say')
 if not k or not say:continue
 label=k.get_text(strip=True)
 if re.match(r'^R-\d+',label):
  num=int(re.match(r'R-(\d+)',label)[1]);content=say.get_text('\n',strip=True).strip('「」');tokens=[]
  for nxt in st.find_next_siblings():
   nk=nxt.select_one('.k')
   if not nk:break
   token=nk.get_text(strip=True)
   if token in assets:tokens.append(token)
   else:break
  intro[num]=(content,tokens)
assert set(intro)==set(range(1,14)),intro.keys()
for num,(content,tokens) in sorted(intro.items()):
 if num==1:tokens=[]
 if num==2:content+=' 實際開放以當天天氣和景區公告為準，不能保證一定上山。'
 if num==11:content='照片為7座商務車參考。2～3位使用7座商務車，由司機兼導遊；4～6位使用9座VIP旅遊車，另配專職導遊。'
 group(f'intro_{num}',content,tokens,True);p['content_sequence'].append(f'intro_{num}')
price='目前標準8天參考團費（人民幣、每人、2人一間）：1～6月及9～12月¥7,580；7～8月¥8,980。單人房差淡季¥1,350、旺季¥2,150，僅單人或主動問單房才說。春節、五一、十一另核，不報普通季節價。7人以上/訂製/兒童無批准價，另確認；不得把7580當包團價。多人優惠只說有優惠，不自算或公開內部折扣。資料未指定報價有效年份，不對任意年份承諾固定價；報目前參考價後確認具體時間。'
group('price_policy',price)
group('rules',guidance)
group('daily_itinerary','8天每日安排：D1昆明接機；D2昆明—洱海生態廊道—鳳陽邑—大理古城；D3大理—喜洲—沙溪；D4沙溪—瀘沽湖—篝火晚會；D5瀘沽湖—里格半島—黑龍潭—麗江古城；D6玉龍雪山—藍月谷—印象麗江—束河古鎮；D7麗江搭動車回昆明—昆明老街；D8昆明送機。住宿以對應酒店或同級確認，不把參考圖片當成確定房型。')
# Keep full source rules and FAQs as traceable knowledge, without stale timer/unsafe absolute promises.
parts=re.split(r'(?=Q\d+：)',texts.split('知識庫・常見問題',1)[1].split('圖片清單',1)[0])
for part in parts:
 m=re.match(r'Q(\d+)：([^\n]+)\n(.+)',part,re.S)
 if not m:continue
 n=int(m[1]);answer=m[3].split('一起發：')[0].strip();answer=answer.split('\n圖片\n')[0]
 if n==3:answer='本線有高海拔段，玉龍雪山冰川公園約4,680米，任何人都可能出現高原不適，不能保證不會高反。請依身體狀況量力而為；有心臟、血壓等問題先請醫師評估，協助確認適合的安排。'
 if n==11:answer='可以協助評估長輩的行程節奏；古鎮多石板路，玉龍雪山有高海拔段。需了解同行年齡與活動需求，有慢性病先請醫師評估，不直接保證適合所有長輩。'
 if n==17:answer='請選擇正規餐廳，不採食不明野生菌。部分毒素不能靠煮熟去除，不以網路評價保證食用安全。'
 group(f'faq_{n}',m[2]+'\n'+answer,[f'圖食YN-{i}' for i in (1,2,3)] if n==7 else ['圖YN-保證'] if n==8 else [])
for n in range(1,7):
 marker=b.find(string=lambda s:s and s.strip()==f'洗對話-{n}')
 st=marker.parent.parent;say=st.select_one('.say')
 if say is None:say=st.find_next(class_='say')
 tokens={1:['圖洗YN-1'],2:['圖洗YN-2'],3:['圖洗YN-3'],4:['圖YN-4'],5:['圖洗YN-5'],6:[]}[n]
 content=say.get_text('\n',strip=True).strip('「」')
 if n==5:content='玉龍雪山冰川公園約4,680米，需注意高原適應，不能保證不會高反。您這邊同行成員大約幾歲呢？我幫您了解合適的行程節奏～'
 group(f'followup_{n}',content,tokens)
# Six seasonal reference groups; dates come from customer preference, never current month substitution.
for months in ('1～2','3～4','5～6','7～8','9～10','11～12'):
 marker=b.find(string=lambda s:s and s.strip()==f'客人說要 {months}月 去')
 if not marker:continue
 st=marker.parent.parent;say=st.select_one('.say') or st.find_next(class_='say')
 content=say.get_text('\n',strip=True).strip('「」').replace('＠客人說的月份＠',months+'月')
 group('season_'+months.replace('～','_'),content)
for token,key in assets.items():
 if token=='圖YN-1':continue
 if not any(key in g['asset_keys'] for g in p['content_groups'].values()):
  a=next(a for a in manifest if a['asset_key']==key);group('material_'+key.removeprefix(route+'_'),a['display_name']+'（季節/實景參考，不保證指定日期景色相同）',[token])
p['followup_policy']={'enabled':True,'checkpoints':{'intro_wait':30,'quote_wait':5,'daily_wait':1440},'daily_sequence':[f'followup_{n}' for n in range(1,7)],'allow_repeat_invitation':False}
large=json.loads(json.dumps(live['packages']['tibet_winter_8d']['large_party']))
large.update(minimum=7,include_explicit_private=False,introduction_sequence=p['content_sequence'][:-1],brief_sequence=['intro_1','intro_10','intro_12'],opening_text='你們7位以上可以安排獨立成團，日期、住宿與景點可以依需求討論～先給您參考經典8天行程，再幫您確認訂製方案。',completed_text='這是雲南經典8天的參考安排，獨立成團的需求和費用我幫您確認～',contact_text='方便留下LINE、微信或WhatsApp的QR Code或ID嗎？我把適合你們的方案整理給您～',unknown_route_text='你們7位以上可以討論獨立成團，想了解雲南哪個方向呢？',handoff_label='高價值・雲南包團',explicit_private_label='雲南訂製',group_text_overrides={},notify_then_followup=False)
large['source_ref']=url
for key in ['followup_policy','unknown_preference_text']:large.pop(key,None)
p['large_party']=large
for a in manifest:
 if a['asset_key']==assets['圖YN-1']:
  a.update(available=False,review_state='needs_review',exclusion_reason='海報寫最多10人，與台灣版最多6人衝突；待業務提供新版，暫用文字每日行程。')
p['ai_guidance']+='\n舊行程海報寫最多10人，與本版6人上限衝突，禁止發送或借用；用daily_itinerary文字。'
skill='---\nname: yunnan-tw-8d\ndescription: 台灣市場雲南昆大麗瀘沽湖8天，2至6人小團、7人以上訂製、季節價格與素材接待。\n---\n\n'+guidance+'\n\n'+price+'\n\n需求處理：介紹結束未有時間用intro_wait；報價後用quote_wait；後續用daily_wait。不得用3小時覆蓋5分鐘規則。6則日追單依序選尚未展示的適用內容；客人已拒絕或已給聯繫方式就停止索取。季節建議依客人月份，沒月份再建議約兩個月後的兩個月區間，11–12月可建議隔年1–2月，並提醒春節需另核。單獨問價格只給符合條件的參考價，不播放全部13段。\n'
(out/'route-package.json').write_bytes((json.dumps(p,ensure_ascii=False,indent=2)+'\n').encode())
(out/'SKILL.md').write_bytes(skill.encode())
(out/'asset-manifest.json').write_bytes((json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode())
(out/'source-sop.txt').write_bytes(texts.encode())
print(json.dumps({'route':route,'assets':len(manifest),'unique_images':len(set(a['sha256'] for a in manifest)),'facts':len(p['knowledge_facts']),'groups':len(p['content_groups']),'intro':len(p['content_sequence'])}))
