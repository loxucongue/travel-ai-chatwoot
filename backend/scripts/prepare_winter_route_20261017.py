"""Build an inactive winter release from the supplied ZIP and saved business text."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT/'data/knowledge/china2go/route-releases/20261017-winter'
RID = 'tibet_winter_8d'
SOURCE = 'website-1013-20261014/content.txt#winter; supplied winter ZIP 2026-10-17'
# Filename matching is semantic: W-12 in the new ZIP is Basum, not the old page's guest photo.
ASSETS = [
    ('map','圖W-2・冬遊西藏地圖.png','冬遊西藏行程圖','itinerary'),
    ('namcha','圖W-4・南迦巴瓦峰.png','南迦巴瓦峰','nyingchi'),
    ('ranwu','圖W-6・然烏湖.png','然烏湖','ranwu'),
    ('ranwu-guests','圖W-7・然烏湖客人合照.png','然烏湖團友照','ranwu'),
    ('laigu','圖W-9・來古藍冰.png','來古藍冰','laigu'),
    ('basum','圖W-12・巴松措.png','巴松措','basum'),
    ('potala','圖W-14・布達拉宮.png','布達拉宮','lhasa'),
    ('qiayang-a','圖W-16・恰央措.png','恰央措冰湖','qiayang'),
    ('qiayang-b','圖W-16-1・恰央措.png','恰央措藍冰','qiayang'),
    ('qiayang-guests','圖W-17・旅客上恰央措.png','恰央措團友照','qiayang'),
    ('qiayang-guests-rest','圖W-18・旅客上恰央措躺著.png','恰央措團友參考照','qiayang'),
    ('hotel-a','圖回1-14 希爾頓1.jpeg','希爾頓客房參考','hotel'),
    ('hotel-b','圖W-21・希爾頓2.jpeg','希爾頓客房參考','hotel'),
    ('vehicle','圖W-25・大VIP車.jpeg','VIP車內座位','vehicle'),
    ('oxygen','圖W-27・車上的彌散氧氣瓶.jpeg','車載供氧設備','oxygen'),
    ('winter-guests','圖W-29・冬天客人合照.png','冬季團友照','followup-guests'),
    ('guide','1追蹤1客人沒回圖・旗子照.jpg','冬季團隊參考照','followup-guide'),
    ('barkhor','2追蹤2沒回圖-八廓街.png','八廓街','followup-barkhor'),
    ('winter-potala','3追蹤-3沒回圖冬天布達拉宮.png','冬季布達拉宮','followup-potala'),
    ('winter-namcha','4追蹤-4沒回圖南嘉巴瓦峰.jpg','南迦巴瓦峰參考照','followup-namcha'),
    ('yamdrok','圖後回W-2・羊湖／卡若拉冰川.png','羊卓雍措','followup-yamdrok'),
    ('potala-guest','圖後回W-4・旱地拔蔥.png','布達拉宮團友照','followup-potala-guest'),
]


def build_package(start, end, *, confirmed=False):
    package = {
        'schema_version':1,'package_version':'2026-10-17.winter-1',
        'knowledge_version':'china2go-peach-two-products-20260829-v1',
        'route_variant':RID,'branch':RID,'name':'冬遊西藏｜冰川藍冰8天',
        'selection_title':'冬遊西藏・冰川藍冰8天',
        'selection_aliases':['冬遊西藏','冬游西藏','冰川藍冰8天','冰川蓝冰8天','林芝冰川冰湖8日'],
        'match_keywords':['冬遊西藏','冬游西藏','冰川藍冰8天','冰川蓝冰8天','林芝冰川冰湖8日'],
        'default_entry_message':'冬遊西藏・冰川藍冰8天',
        'required_slots':['party_size','departure_window'], 'initial_delivery_interval_seconds':2,
        'publication':{'status':'prepared' if confirmed else 'pending_business_confirmation',
                       'activation_allowed':confirmed,'production_enabled':False},
        'source':{'branch_content':SOURCE,'archive':'-12-2月選項-冬遊西藏.zip'},
        'intake':{'field':'departure_window','question':'請問您大概想幾月出發呢？',
                  'options':['12月','1月','2月','春節'],'wait_seconds':30},
        'ai_guidance':'本線林芝進拉薩出，不含珠峰、絨布旅館或珠峰小鎮。已知月份不重問，先核日期再介紹。'
            '以行程圖和本線素材為準，不把舊網頁編號當新圖編號。沒有問價格不報價；'
            '普通房差1500僅主動問單住時答，春節特價房差未確認不能套用。'
            '不承諾氧氣濃度、時數、醫療效果、冰況或所有團均可上冰。'
            '沿用每日一次與靜默交接；不使用半天/三小時索資、已讀續推或檔案太大的舊說法。',
        'seasonal_pricing':{'years':sorted({int(start[:4]),int(end[:4])}),
            'operating_periods':[[start,end]],'minimum_notice_days':30,'weekday':3,
            'holiday_ranges':[['02-01','02-16']],'advisor_months':[], 'unavailable_ranges':[],
            'private_departure_min_party_size':4,
            'bands':[{'script_id':'price_winter','ranges':[['01-01',end[5:]], [start[5:],'12-31']],
                      'price_cny':9280,'single_supplement_cny':1500}]},
        'knowledge_facts':[], 'content_groups':{}, 'content_sequence':[], 'fixed_answers':[]}
    facts={
        'itinerary':'林芝接機；色季拉山、南迦巴瓦峰、魯朗林海；然烏湖、來古冰川；噶朗湖、噶朗村、噶朗王宮、雪山牧場；秀巴古堡、巴松措；布達拉宮、大昭寺、八廓街；羊卓雍措、卡若拉冰川、恰央措；拉薩送機/送火車。不含珠峰或拉日鐵路。',
        'hotel':'行程圖：林芝希爾頓花園、波密山水供氧酒店、拉薩希爾頓惠庭。雙人一間。冰川線不住絨布或珠峰小鎮；具體房型以安排確認為準。',
        'group':'4–6人拼團，一人也可報名，VIP車。已確認普通團4人獨立成團每人加400、6人原價；春節及其他未核實組合不套用。',
        'included':'包含入藏函、車、司機、導遊、保險、住宿、門票、酒店早餐；不含機票、午晚餐、小費。普通團單人房差1500元，僅客人主動詢問時說明。',
        'equipment':'車上配備供氧設備、緊急氧氣鋼瓶和葡萄糖。設備不是醫療效果保證；不承諾氧氣濃度或供應時數。',
        'booking':f'待確認生效期間為{start}至{end}，每週四；普通團9280元/人，兩人一間。價格引用需先核日期，入藏函按業務要求至少提前30天。' if not confirmed else f'已確認出發期間{start}至{end}，普通每週四9280元/人，兩人一間；指定特別日期以special_departures為準。入藏函至少提前30天。',
        'no_shopping':'無購物寫入合約，有購物行為依約賠償人民幣5000元。',
    }
    package['knowledge_facts']=[{'id':RID+'.'+k,'text':v,'source_ref':SOURCE} for k,v in facts.items()]
    groups={
        'itinerary':('先給您看冬遊西藏的冰川藍冰8天行程～從林芝進、拉薩出，不走珠峰。','itinerary'),
        'nyingchi':('行程從林芝出發，會到魯朗林海並遠眺南迦巴瓦峰，景觀視當天天氣而定。','itinerary'),
        'ranwu':('我們會去到G318沿線的然烏湖，給您看看湖景與團友照片～','itinerary'),
        'laigu':('這條冬遊線會去來古冰川，給您看看藍冰的參考照片～','itinerary'),
        'basum':('沿途也安排巴松措，湖心島與雪山都是這裡的特色。','itinerary'),
        'lhasa':('到拉薩會安排布達拉宮、大昭寺與八廓街，看看當地的人文風景。','itinerary'),
        'qiayang':('行程也加入恰央措冰湖，這些是團友的參考照片，實際冰況及活動依當天安排。','itinerary'),
        'hotel':('林芝與拉薩安排希爾頓系列酒店，波密住山水供氧酒店，給您看看客房參考照。','hotel'),
        'vehicle':('我們採4–6人小團，安排VIP車，給您看車內座位～','group'),
        'oxygen':('車上備有供氧設備、緊急氧氣鋼瓶與葡萄糖。','equipment'),
        'no_shopping':('我們把無購物寫在合約上，有購物行為依約賠償人民幣5,000元。','no_shopping'),
    }
    for key,caption,evidence,initial in [(k,*v,True) for k,v in groups.items()] + [
        (g,label+'，給您看看參考照片～','itinerary',False) for _,_,label,g in ASSETS if g.startswith('followup-')]:
        package['content_groups'][key]={'approved_text':caption,'asset_keys':[RID+'_'+a for a,_,_,g in ASSETS if g==key],
            'evidence_refs':[RID+'.'+evidence],'initial_delivery':initial,'delivery_mode':'text_then_assets','purpose':key}
    package['content_groups']['price_reference']={'approved_text':'請問您預計哪一年、幾月出發呢？',
        'asset_keys':[],'evidence_refs':[RID+'.booking'],'initial_delivery':False,'delivery_mode':'text_only'}
    package['content_sequence']=list(package['content_groups'])
    for key,name,text in [('price_winter','冬遊普通團報價',
        f'【冬遊西藏｜冰川藍冰8天】\n冬季出發：{start.replace("-", "/")}–{end.replace("-", "/")}（每週四，春節另計）\n'
        '6人精緻小團：¥9,280/人（2人一間）\n包含：入藏函、車、司機、導遊、保險、住宿、門票、酒店早餐\n不含：機票、午晚餐、小費'),
        ('quote_date','確認日期','請問您預計哪一年、幾月出發呢？')]:
        package['fixed_answers'].append({'id':key,'name':name,'answer_text':text,'source_ref':SOURCE,
            'status':'active','answer_origin':'operator_approved','content_group_key':'price_reference',
            'topics':['price'],'fact_ids':[RID+'.booking'],'asset_ids':[],
            'positive_examples':['冬遊團期與價格'],'negative_examples':['常規8天','其他產品'],
            'usage_note':'價格需get_departure_quote核對成功才引用，輕鬆問句另放一條；春節不能用普通價。'})
    return package


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--start',default='2026-12-15')
    parser.add_argument('--end',default='2027-02-28')
    parser.add_argument('--confirmed',action='store_true')
    args=parser.parse_args()
    RELEASE.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.archive) as z:
        members={Path(i.filename if i.flag_bits&0x800 else i.filename.encode('cp437').decode('big5')).name:i
                 for i in z.infolist() if not i.is_dir()}
        manifest=[]
        for key,filename,label,group in ASSETS:
            data=z.read(members[filename]);digest=hashlib.sha256(data).hexdigest()
            destination=RELEASE/'assets'/(key+Path(filename).suffix.lower())
            destination.parent.mkdir(exist_ok=True);destination.write_bytes(data)
            manifest.append({'asset_key':RID+'_'+key,'route_variant':RID,'source_archive':args.archive.name,
                'source_filename':filename,'relative_path':destination.relative_to(RELEASE).as_posix(),
                'sha256':digest,'byte_size':len(data),'display_name':label,'content_group_key':group,
                'content_family':RID+'.'+key,'what_it_shows':label,'recommended_caption':label,
                'review_state':'verified_source_and_zip','available':False})
    package=build_package(args.start,args.end,confirmed=args.confirmed)
    sys.path.insert(0,str(ROOT/'backend'))
    from scripts.configure_winter_spring_20261017 import update_package
    from app.route_packages import _validate
    package=update_package(package,confirmed_specials=args.confirmed)
    _validate(package,Path('winter-package.json'))
    dest=RELEASE/'tibet-winter-8d/route-package.json';dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(package,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (RELEASE/'asset-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'assets':len(manifest),'publication':package['publication']}))


if __name__=='__main__':main()
