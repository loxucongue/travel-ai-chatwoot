"""Install Beijing using the existing package/material/configuration stores.

Caller owns the deployment lock and database/file backups. No conversation or
channel is modified. Repeating this release is idempotent; conflicting assets fail.
"""
import hashlib
import json
import mimetypes
from pathlib import Path

ROUTE = 'beijing_micro_5d'


def install(db, release, *, tenant_id, user_id, activate=False):
    from sqlalchemy import select
    from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
    from app import route_packages as catalog
    from app.reception_config import get_reception_configuration, put_reception_configuration, ReceptionConfiguration
    release = Path(release).resolve()
    package = json.loads((release / 'route-package.json').read_text(encoding='utf8'))
    manifest = json.loads((release / 'asset-manifest.json').read_text(encoding='utf8'))
    assert package['route_variant'] == ROUTE
    for asset in manifest:
        path = (release / asset['path']).resolve()
        if not path.is_relative_to(release) or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('beijing_asset_integrity_failed')
    version = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id == tenant_id,
                                                       KnowledgeVersion.version_key == package['knowledge_version']))
    if not version:
        raise ValueError('beijing_knowledge_version_missing')
    target = catalog.RUNTIME_PACKAGE_ROOT / ROUTE / 'route-package.json'
    previous = target.read_bytes() if target.exists() else None
    installed = catalog.install_route_package(package)
    try:
        config = get_reception_configuration(db)
        for item in manifest:
            path = release / item['path']
            asset = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id,
                                                          MaterialAsset.asset_key == item['asset_key']))
            if asset:
                if asset.file_hash != item['sha256']:
                    raise ValueError('beijing_existing_asset_conflict:' + item['asset_key'])
                if activate:
                    asset.metadata_json = {**asset.metadata_json, 'review_state': 'approved', 'live_approved': True}
                continue
            media = StoredMedia(tenant_id=tenant_id, created_by=user_id, original_name=item['source_filename'],
                                media_type='image', mime_type=mimetypes.guess_type(path.name)[0],
                                file_size=item['byte_size'], storage_path=str(path))
            db.add(media)
            db.flush()
            db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=item['asset_key'],
                                 source_path=str(path), display_name=item['display_name'], media_type='image',
                                 file_hash=item['sha256'], available=True, usage=item['content_group_key'],
                                 metadata_json={**item, 'stored_media_id': media.id,
                                                'content_family': 'asset:' + item['sha256'],
                                                'review_state': 'approved' if activate else 'pending',
                                                'live_approved': activate}))
        if activate and ROUTE not in config['routing']['enabled_route_variants']:
            config['routing']['enabled_route_variants'].append(ROUTE)
        # Each daily stage is available to the existing one-per-day scheduler.
        rules = {r['id']: r for r in config.get('material_rules', [])}
        for n in range(1, 7):
            group = package['content_groups'][f'followup_{n}']
            if not group['asset_keys']:
                continue
            key = f'beijing_daily_{n}'
            rules[key] = {'id': key, 'name': f'北京每日分享{n}', 'enabled': activate,
                          'purpose': 'followup', 'route_variants': [ROUTE], 'any_labels': [],
                          'scenario': f'北京第{n}組每日分享；僅前序分享已完成且本組未發過、未拒絕聯絡、無人工接管時。一天最多一組。',
                          'approved_text': group['approved_text'], 'asset_keys': group['asset_keys']}
        config['material_rules'] = list(rules.values())
        put_reception_configuration(db, ReceptionConfiguration.model_validate(config))
        db.flush()
        return {'route': ROUTE, 'assets': len(manifest), 'enabled': activate, 'package_path': str(installed)}
    except Exception:
        db.rollback()
        if previous is None:
            installed.unlink(missing_ok=True)
        else:
            installed.write_bytes(previous)
        catalog.reload_route_packages()
        raise
