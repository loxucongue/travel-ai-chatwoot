"""Add business-approved Tibet follow-up content without changing cadence/history."""
from copy import deepcopy
import hashlib
import mimetypes
from pathlib import Path

ROUTES = ('peach_9d_2027', 'peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d', 'tibet_winter_8d')
EVEREST = ('peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d')
SOURCE = 'business-followup-20261019（業務提供2026-10-14話術截圖、1.jpg、3.jpg、5.png）'
PREFIX = 'followup_20261019_'
TEA = ('✨ 布達拉宮觀景下午茶＋藏裝拍攝，都是我們行程標配、免費贈送的體驗喔！\n'
       '有風景、有體驗，又不趕行程的旅行，您會心動嗎？ ❤️')
STARS = ('✨ 分享一張珠峰大本營的真實星空照片，滿天繁星，超美的～\n'
         '這樣的風景，真的值得親自來感受一次！實際觀星需視當天天氣而定。')
TRUST = ('我們的理念很簡單～旅遊就應該是一場純粹的體驗！ ❤️\n'
         '放心看風景，把時間留給真正的西藏體驗！\n'
         '我們堅持0購物、0隱藏消費，不安排氧氣店、高反「醫生」打針、藏藥、天珠唐卡等購物點。\n'
         '承諾會白紙黑字寫進正規旅遊合同裡，讓您玩得安心、放心！ ✨')
PHOTOS = {
 'tea': {'key': 'tibet-followup-potala-tea-20261019', 'filename': '1.jpg',
         'hash': '12d502788709284021daf9900cdf184a5375dbb2141a6f95cb1f2b210f27db7c',
         'routes': ROUTES, 'name': '布達拉宮觀景下午茶', 'caption': TEA,
         'shows': '旅客坐在觀景平台休憩，背景為布達拉宮；照片未展示藏裝拍攝。',
         'avoid': ['不將照片說成當天拍攝或當前天氣', '不憑照片增加茶點規格、攝影張數或服裝套數']},
 'stars': {'key': 'tibet-followup-everest-stars-20261019', 'filename': '3.jpg',
           'hash': '42190b64fd95b807484c5bb8d0a3aa5f15c67856fba943af24f676d98a3a3333',
           'routes': EVEREST, 'name': '珠峰大本營星空', 'caption': STARS,
           'shows': '業務提供的珠峰大本營星空照片，畫面可見繁星、山谷與旗杆。',
           'avoid': ['拍攝日期未核實，不稱這幾天、今天或近期實拍', '不保證每次出發有星空或一定可觀星',
                     '不承諾本次行程夜宿珠峰大本營', '不向不含珠峰的桃花9日、冬遊冰川8日推薦此景為本線內容']},
 'trust': {'key': 'tibet-followup-essence-guarantee-20261019', 'filename': '5.png',
           'hash': 'e6eedf37178afde00bb5cf0fabc7533977db6e5e50d6ab519fa5c3788a97c11f',
           'routes': ('tibet_essence_8d',), 'name': '精華8日承諾與保證', 'caption': TRUST,
           'shows': '業務提供的西藏希爾頓精華8日保證圖，含無購物違約賠償人民幣5000元條款。',
           'avoid': ['圖片僅用於精華8日，不將圖中住宿、車型及天數承諾套用其他線路',
                     '0隱藏消費不是費用全包；明列不含的機票、午晚餐、小費等仍依報價及合同']},
}


def package_delta(original):
    p = deepcopy(original)
    route = p['route_variant']
    if route not in ROUTES:
        return p
    topics = [('tea', TEA), *([('stars', STARS)] if route in EVEREST else []), ('trust', TRUST)]
    for topic, text in topics:
        key = PREFIX + topic
        photo = PHOTOS[topic]
        assets = [photo['key']] if route in photo['routes'] else []
        fact = {'id': route + '.' + key, 'text': text + '\n使用邊界：' + '；'.join(photo['avoid']), 'source_ref': SOURCE}
        # Other routes share the approved no-shopping text, not the 8-day image promises.
        if topic == 'trust' and not assets:
            fact['text'] = TRUST + '\n0隱藏消費不代表費用全包；明列不含項目仍依本線報價及合同。'
        p['knowledge_facts'] = [f for f in p['knowledge_facts'] if f['id'] != fact['id']] + [fact]
        p['content_groups'][key] = {'purpose': '沉默跟進：' + photo['name'], 'approved_text': text,
            'asset_keys': assets, 'evidence_refs': [fact['id']], 'initial_delivery': False,
            'delivery_mode': 'text_then_assets' if assets else 'text_only'}
        if key not in p['content_sequence']:
            p['content_sequence'].append(key)
        answer = {'id': key, 'name': '沉默跟進：' + photo['name'], 'answer_text': text,
            'source_ref': SOURCE, 'content_group_key': key, 'topics': ['silence_followup'],
            'fact_ids': [fact['id']], 'asset_ids': assets, 'status': 'active', 'answer_origin': 'operator_approved',
            'positive_examples': ['silence_due：本線客戶沉默，這組話術與圖片尚未發送'],
            'negative_examples': ['開場介紹', '已發送這組內容', '正在回答客戶具體問題', '人工接管或拒絕聯繫'],
            'usage_note': '普通沉默跟進優先依次選未發送的下午茶、珠峰星空（如本線適用）、純玩承諾，每輪只一組。'
                          '使用本組原話，不附加留資或催日期人數。asset_ids僅在followup_materials中仍可用時發送；'
                          '其他線路不能借用本組圖片。已發送話術不重發；不得承諾拍攝時點、天氣或未確認服務。'}
        p['fixed_answers'] = [s for s in p.get('fixed_answers', []) if s['id'] != key] + [answer]
    suffix = '.followup-20261019'
    if not p['package_version'].endswith(suffix):
        p['package_version'] += suffix
    return p


def configure_assets(db, media_dir, files, *, photos=None, prefix=PREFIX, source=SOURCE):
    from sqlalchemy import select
    from app.models import KnowledgeVersion, MaterialAsset, StoredMedia, User
    from app.route_packages import KNOWLEDGE_VERSION
    from app.material_library import replace_asset_binding
    version = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id == 1,
                         KnowledgeVersion.version_key == KNOWLEDGE_VERSION))
    user = db.scalar(select(User).where(User.role == 'super_admin', User.active.is_(True)).order_by(User.id))
    assert version and user
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for topic, spec in (PHOTOS if photos is None else photos).items():
        raw = Path(files[topic]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == spec['hash'], 'source_photo_changed'
        path = media_dir / (spec['hash'] + Path(spec['filename']).suffix)
        if path.exists():
            assert path.read_bytes() == raw
        else:
            path.write_bytes(raw)
        media = db.scalar(select(StoredMedia).where(StoredMedia.tenant_id == 1, StoredMedia.storage_path == str(path)))
        if not media:
            media = StoredMedia(tenant_id=1, created_by=user.id, original_name=spec['name'] + path.suffix,
                media_type='image', mime_type=mimetypes.guess_type(path.name)[0], file_size=len(raw), storage_path=str(path))
            db.add(media); db.flush()
        asset = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id,
                           MaterialAsset.asset_key == spec['key']))
        if not asset:
            asset = MaterialAsset(knowledge_version_id=version.id, asset_key=spec['key'], source_path=str(path),
                display_name=spec['name'], usage='silence_followup', media_type='image',
                available=True, metadata_json={})
            db.add(asset); db.flush()
        replace_asset_binding(db, asset, media, metadata_updates={
            'route_variants': list(spec['routes']), 'content_group_key': prefix + topic,
            'content_family': 'tibet:followup:' + topic, 'review_state': 'approved', 'live_approved': True,
            'source': source, 'source_filename': spec['filename'], 'sha256': spec['hash'], 'byte_size':len(raw),
            'what_it_shows': spec['shows'], 'feature_points': [spec['name']],
            'recommended_caption': spec['caption'], 'customer_value': '了解西藏旅程體驗與純玩承諾。',
            'avoid_claims': spec['avoid'],
        })
        result.append({'key': asset.asset_key, 'media_id': media.id, 'hash': asset.file_hash, 'routes': list(spec['routes'])})
    return result
