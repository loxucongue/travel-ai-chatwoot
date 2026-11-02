"""Idempotent Ali/private-group configuration using the approved original media."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[2]
RELEASE=ROOT/'data/knowledge/china2go/route-releases/20261102-ali'
ALI='tibet_ali_15d'
HIGHLIGHTS={
    'peach_11d_2027':('private_peach_castle','桃花節期間可看秀巴古堡、桃花與雪山；這條11天還會上珠峰。獨團的日期、天數與景點可以和顧問討論。'),
    'peach_9d_2027':('private_peach_castle','這條桃花9天不上珠峰，一樣能看秀巴古堡、桃花與沿途雪山。獨團可以和顧問討論步調及安排。'),
    'tibet_essence_8d':('private_everest_sunset','這是珠峰日照金山的畫面。這條8天包含拉薩與珠峰；你們自己一團，景點停留與住宿可以和顧問核對。'),
    'tibet_overview_10d':('private_everest_guests','這是客人在珠峰大本營的合照。10天從林芝進藏，一路到拉薩、珠峰，獨團可以討論日期、天數和想加的景點。'),
    'tibet_linzhi_8d':('private_basum','這是巴松措湖心島。林芝8天從較低海拔進藏、不上珠峰，適合想把步調放慢的客人；仍須依身體狀況評估高原旅行。'),
    'tibet_winter_8d':('private_winter_ice','這是冬遊會去的恰央措，冰面與冰裂紋路很特別。實際能否上冰要依現場安全及天氣安排，不保證特定冰況。'),
    ALI:('private_ali_wuma','這是阿里北線的物瑪措湖中公路。阿里15天把轉山、三大聖湖與藏北串起來，獨團細節由阿里顧問依你們的需求規劃。'),
}


def update_package(original):
    p=deepcopy(original);route=p['route_variant']
    if route not in HIGHLIGHTS:return p
    groups=p['content_groups'];key,text=HIGHLIGHTS[route]
    source='https://china2go.com/8425-2/ (2026-10-27; explicit private scope confirmed 2026-11-01)'
    fact_id=route+'.private_introduction'
    facts=[f for f in p['knowledge_facts'] if f['id']!=fact_id]
    facts.append({'id':fact_id,'text':text+' 我們是在地的團隊，每一季實際踩線、與導遊優化行程。獨團車型、報價與日期由顧問核對。','source_ref':source})
    p['knowledge_facts']=facts
    for group,text,assets in [
        ('private_highlight',text,[key]),
        ('private_guests','分享我們客人的旅遊照片～你們自己一團，可以用基本路線延伸討論；我們會依實際日期、人數與需求核對安排。',['private_guests']),
        ('private_advantage','我們是在地的團隊，每一季都會實際踩線，和導遊一起優化行程與酒店。日期、天數和想加的景點，可以請顧問與你們一起規劃。',[])]:
        groups[group]={'approved_text':text,'asset_keys':assets,'evidence_refs':[fact_id],
                       'initial_delivery':False,'delivery_mode':'assets_then_text' if assets else 'text_only'}
    overview=next(k for k in ('itinerary_overview','itinerary') if k in groups)
    policy=deepcopy(p.get('large_party') or {})
    policy.update(enabled=True,minimum=7,include_explicit_private=True,source_ref=source,
        introduction_sequence=[overview,'private_highlight','private_guests','private_advantage'],
        brief_sequence=[overview,'private_highlight','private_guests','private_advantage'],
        group_text_overrides={overview:'先給您看基本行程圖～你們自己一團，日期、天數與想去的景點可以再和顧問討論。'},
        opening_text='您們{party_description}的行程，我們會按獨立一團的方向確認～',
        completed_text='剛剛看的是基本行程，您們自己一團的日期、天數和景點，可以再和顧問討論。',
        contact_text='您方便加入我們的官方LINE@（@china2go）或微信（china2go-cits）嗎？也可以提供您的LINE或微信QR code，方便顧問和您確認安排。',
        unknown_route_text='您們可以安排自己獨立一團～請問這次規劃幾天，或有特別想去的地方嗎？',
        unknown_preference_text='您們有特別想去的地方，或想避開的景點嗎？',
        notify_then_followup=True,
        followup_policy={'enabled':True,'lead_priority_override':True,
          'checkpoints':{'peach_interest_wait':5,'preference_wait':720,'advisor_wait':720,'daily':1440},
          'reference_preview_checkpoints':['peach_interest_wait','preference_wait'],
          'daily_sequence':['private_guests','private_highlight'],
          'guidance':'3–4月但未表達桃花意向先問，peach_interest_wait五分鐘後只推薦相符參考行程，不能視為同意選線。未知天數/偏好用preference_wait半天後推薦，不因12天以上推阿里。介紹後已通知顧問、未認領且仍有AI權限時advisor_wait半天後分享未發客人照；邀請最多一次，不重複催留資。之後每日符合需求的新素材；人工接手立即停止。'})
    p['large_party']=policy
    p['package_version']=p['package_version'].split('+ali-series-')[0]+'+ali-series-20261102'
    return p


def media_sources(archive_dir):
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    files={}
    for name in dict.fromkeys(a['source_archive'] for a in manifest):
        with zipfile.ZipFile(Path(archive_dir)/name) as z:
            by_hash={hashlib.sha256(raw).hexdigest():raw for item in z.infolist() if not item.is_dir()
                     for raw in [z.read(item)]}
            for item in (a for a in manifest if a['source_archive']==name):
                raw=by_hash[item['sha256']]
                if len(raw)!=item['byte_size']:raise ValueError('media_size_mismatch')
                files[item['asset_key']]=raw
    return manifest,files


def install_assets(db, manifest, files, media_dir, tenant_id=1, first_media_id=None):
    from sqlalchemy import select
    from app.models import KnowledgeVersion,MaterialAsset,User
    from app.route_packages import KNOWLEDGE_VERSION
    from scripts.stage_tibet_routes_20261014 import install_materials
    version=db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id==tenant_id,
                    KnowledgeVersion.version_key==KNOWLEDGE_VERSION))
    user=db.scalar(select(User).where(User.role=='super_admin',User.active.is_(True)).order_by(User.id))
    if not version or not user:raise ValueError('tenant_knowledge_or_operator_missing')
    existing={a.asset_key:a for a in db.scalars(select(MaterialAsset).where(MaterialAsset.knowledge_version_id==version.id))}
    for spec in manifest:
        if spec['asset_key'] in existing and existing[spec['asset_key']].file_hash != spec['sha256']:
            raise ValueError('existing_release_asset_changed')
    # Rerunning a release must preserve revocation and historical bindings.
    missing=[spec for spec in manifest if spec['asset_key'] not in existing]
    install_materials(db,missing,files,Path(media_dir),tenant_id,user.id,version.id,first_media_id=first_media_id)
    for spec in manifest:
        asset=db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id==version.id,MaterialAsset.asset_key==spec['asset_key']))
        asset.metadata_json={**asset.metadata_json,'route_variants':spec['route_variants'],
            'content_family':'asset:'+spec['asset_key'],
            'what_it_shows':spec['what_it_shows'],'customer_value':'了解本線真實景點與旅遊安排',
            'avoid_claims':['不保證天氣、野生動物、庫存或身體適應；圖片不代表已訂位。']}
    db.flush()


def material_rules(packages):
    result=[]
    for route,p in packages.items():
        if route not in HIGHLIGHTS:continue
        keys=[k for k in p['content_groups'] if k.startswith('followup_')] if route==ALI else ['private_guests','private_highlight']
        for order,key in enumerate(keys):
            group=p['content_groups'][key]
            result.append({'id':route+'_'+key,'name':p['name']+'・'+key,'enabled':True,'purpose':'followup',
                'route_variants':[route],'any_labels':[] if route==ALI else ['高價值・獨立成團'],'asset_keys':group['asset_keys'],
                'approved_text':group['approved_text'],'scenario':f'本系列跟進順序{order+1}，按對話選尚未發送的主題；不重複圖片或已邀請的聯絡方式。'})
    return result
