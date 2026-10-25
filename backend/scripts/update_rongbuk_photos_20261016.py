"""Approved photo/config delta; no Chatwoot or model calls."""
from copy import deepcopy
import hashlib
import json
import mimetypes
from pathlib import Path
from uuid import uuid4

TARGETS = {
    'peach_11d_2027': ('rongbuk_reference', 'routes12-rongbuk-room'),
    'tibet_essence_8d': ('rongbuk', 'tibet_essence_8d_rongbuk'),
    'tibet_overview_10d': ('rongbuk', 'tibet_overview_10d_rongbuk'),
}
BATHROOM_KEY = 'rongbuk-bathroom-20261016'
ROOM_HASH = '647c1d2962263336c00ad477316837cb5abf4f2071b2c7f3ab385ecc0d2e4a4d'
BATHROOM_HASH = 'bd20dd87b98e28d718bb80f80ce6ab7d8ec04b2dcf4598fda78888b249911e97'


def package_delta(original):
    package = deepcopy(original)
    route = package['route_variant']
    if route not in TARGETS:
        return package
    group, room = TARGETS[route]
    package['content_groups'][group]['asset_keys'] = [room, BATHROOM_KEY]
    for answer in package.get('fixed_answers', []):
        if room in answer.get('asset_ids', []):
            answer['asset_ids'] = [room, BATHROOM_KEY]
    if not package['package_version'].endswith('.rongbuk-20261016'):
        package['package_version'] += '.rongbuk-20261016'
    return package


def refresh_drafts(db, row):
    """Rebind only unsent room drafts; delivered history remains immutable."""
    from app.material_library import resolve_materials, tenant_for_session
    tenant = tenant_for_session(db, row)
    if tenant is None:
        return 0
    room_keys = {v[1] for v in TARGETS.values()}
    original = list(row.messages)
    updated = []
    changed = 0
    for part in original:
        route = part.get('route_variant') or row.controls.get('route_variant')
        if part.get('status') != 'draft' or part.get('asset_key') not in room_keys or route not in TARGETS:
            updated.append(part)
            continue
        room, bathroom = resolve_materials(db, [part['asset_key'], BATHROOM_KEY], route, tenant)
        replacement = {**part, **room}
        updated.append(replacement)
        changed += replacement != part
        if not any(m.get('asset_key') == BATHROOM_KEY and m.get('plan_id') == part.get('plan_id') for m in original):
            updated.append({**replacement, **bathroom, 'id': 'v3-' + uuid4().hex})
            changed += 1
    if changed:
        row.messages = updated
    return changed


def configure(db, media_dir, files):
    from sqlalchemy import select
    from app.models import MaterialAsset, StoredMedia, KnowledgeVersion, User
    from app.automation_models import AutomationSession
    from app.material_library import replace_asset_binding
    from app import route_packages as catalog
    from app.operations import audit
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    version = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id == 1,
                        KnowledgeVersion.version_key == catalog.KNOWLEDGE_VERSION))
    user = db.scalar(select(User).where(User.role == 'super_admin', User.active.is_(True)).order_by(User.id))
    assert version and user
    media = {}
    for kind, expected in [('room', ROOM_HASH), ('bathroom', BATHROOM_HASH)]:
        source = Path(files[kind])
        raw = source.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        # The supplied room photo is already used by the 11-day itinerary.
        existing = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id,
                            MaterialAsset.file_hash == expected))
        stored = db.get(StoredMedia, existing.metadata_json.get('stored_media_id')) if existing else None
        if stored:
            assert hashlib.sha256(Path(stored.storage_path).read_bytes()).hexdigest() == expected
        else:
            path = media_dir / (expected + source.suffix.lower())
            if path.exists():
                assert path.read_bytes() == raw
            else:
                path.write_bytes(raw)
            stored = db.scalar(select(StoredMedia).where(StoredMedia.tenant_id == 1, StoredMedia.storage_path == str(path)))
            if not stored:
                stored = StoredMedia(tenant_id=1, created_by=user.id, original_name='絨布旅館-' + kind + source.suffix.lower(),
                    media_type='image', mime_type=mimetypes.guess_type(path.name)[0], file_size=len(raw), storage_path=str(path))
                db.add(stored); db.flush()
        media[kind] = stored
    binding_ids = []
    for route, (group, key) in TARGETS.items():
        asset = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id, MaterialAsset.asset_key == key))
        assert asset and asset.available and asset.metadata_json.get('live_approved')
        replace_asset_binding(db, asset, media['room'], metadata_updates={
            'source': 'business-approved-rongbuk-20261016', 'source_archive': '', 'source_filename': Path(files['room']).name,
            'sha256': ROOM_HASH, 'byte_size': media['room'].file_size, 'width': 744, 'height': 914,
            'what_it_shows': '業務確認的絨布旅館雙床客房照片（2026-10-16）。',
            'content_family': 'hotel:rongbuk:room', 'available': True,
        })
        binding_ids.append(asset.id)
    asset = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id, MaterialAsset.asset_key == BATHROOM_KEY))
    if not asset:
        asset = MaterialAsset(knowledge_version_id=version.id, asset_key=BATHROOM_KEY,
            source_path=media['bathroom'].storage_path, display_name='絨布旅館獨立衛浴', usage='rongbuk_bathroom', media_type='image', available=True, metadata_json={})
        db.add(asset); db.flush()
    replace_asset_binding(db, asset, media['bathroom'], metadata_updates={
        'route_variants': list(TARGETS), 'content_group_key': 'rongbuk_bathroom', 'content_family': 'hotel:rongbuk:bathroom',
        'review_state': 'approved', 'live_approved': True, 'source': 'business-approved-rongbuk-20261016',
        'source_filename': Path(files['bathroom']).name, 'sha256': BATHROOM_HASH,
        'byte_size': media['bathroom'].file_size, 'width': 1279, 'height': 1706,
        'what_it_shows': '業務提供的絨布旅館衛浴照片，可見洗手台、馬桶及熱水器。',
        'feature_points': ['洗手台', '馬桶', '熱水器'],
        'customer_value': '讓客戶了解珠峰段住宿的衛浴環境。',
        'recommended_caption': '這張是絨布旅館的獨立衛浴，給您參考～',
        'avoid_claims': ['不能憑照片保證所有房型與餘房', '不憑照片推斷供氧指標或醫療效果', '冬季改住珠峰小鎮時不以此圖代表入住住宿'],
    })
    for route in TARGETS:
        catalog.install_route_package(package_delta(catalog.ROUTE_PACKAGES[route]))
    changed = {r.id: count for r in db.scalars(select(AutomationSession).where(AutomationSession.engine_version == 'v3'))
               if (count := refresh_drafts(db, r))}
    audit(db, user, 'material.rongbuk_photos_updated', 'route', 'rongbuk',
          {'route_variants': list(TARGETS), 'room_asset_ids': binding_ids, 'bathroom_asset_id': asset.id, 'draft_updates': changed})
    return {'routes': list(TARGETS), 'room_media_id': media['room'].id, 'bathroom_media_id': media['bathroom'].id,
            'draft_updates': changed, 'customer_messages_sent': 0}


def verify(db):
    from app.reception_v3.skills import compile_skills
    from app.reception_v3.runtime import Decision
    from app.reception_v3.service import parts_for
    from app.reception_v3.seasonal_quotes import departure_quote
    from app.material_library import resolve_materials
    from app.automation_models import AutomationSession
    from app.models import InboxBinding
    from sqlalchemy import select
    bundle = compile_skills(db)
    inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == 1).order_by(InboxBinding.id))
    checks = []
    for route, (group, key) in TARGETS.items():
        assert bundle['routes'][route]['groups'][group]['assets'] == [key, BATHROOM_KEY]
        resolved = resolve_materials(db, [key, BATHROOM_KEY], route, 1)
        assert [m['media_hash'] for m in resolved] == [ROOM_HASH, BATHROOM_HASH]
        row = AutomationSession(inbox_binding_id=inbox.id, controls={'route_variant': route}, messages=[])
        _, parts = parts_for(db, row, bundle, Decision(route_variant=route, start_introduction=True).model_dump())
        photos = [p['media_hash'] for p in parts if p.get('media_hash') in (ROOM_HASH, BATHROOM_HASH)]
        assert photos == [ROOM_HASH, BATHROOM_HASH], (route, photos)
        # The same resolver is used for an explicit accommodation/follow-up answer.
        decision = Decision.model_validate({'route_variant': route, 'messages': [{'asset_keys': [key, BATHROOM_KEY]}]}).model_dump()
        assert [p['media_hash'] for p in parts_for(db, row, bundle, decision)[1]] == photos
        row.messages = [{'id': 'delivered', 'status': 'submitted', 'asset_key': key, 'media_hash': 'original'},
                        {'id': 'draft', 'status': 'draft', 'asset_key': key, 'plan_id': 'one', 'route_variant': route}]
        assert refresh_drafts(db, row) == 2
        assert row.messages[0]['media_hash'] == 'original'
        assert [p['media_hash'] for p in row.messages[1:]] == photos
        assert refresh_drafts(db, row) == 0
        checks.append({'route': route, 'initial_intro': photos, 'followup_answer': photos, 'draft_rebinding_and_no_replay': True})
    row = AutomationSession(inbox_binding_id=inbox.id, controls={'route_variant': 'peach_9d_2027'}, messages=[])
    _, parts = parts_for(db, row, bundle, Decision(route_variant='peach_9d_2027', start_introduction=True).model_dump())
    assert not any(p.get('media_hash') in (ROOM_HASH, BATHROOM_HASH) for p in parts)
    for route in ('tibet_essence_8d', 'tibet_overview_10d'):
        winter = departure_quote(bundle, route, '2027-01-15', '', '2026-10-16T00:00:00Z')
        assert winter['next_action'] == 'offer_winter_recommendation'
    return {'passed': True, 'routes': checks, 'nine_day_excluded': True, 'winter_recommendation_preserved': True}
