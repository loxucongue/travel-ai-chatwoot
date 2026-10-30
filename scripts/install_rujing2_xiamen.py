"""Install the supplied campaign into an existing, stopped-send rujing2 database.

Run from backend/ with the server's .env.rujing2. Default is read-only preflight;
--apply makes a SQLite backup and imports the campaign without resetting users.
Stop API/playground workers during apply so they cannot observe a partial import.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'businesses/rujing2/campaigns/xiamen-20261029'
MARKER = 'campaign_install:xiamen-20261029'
sys.path.insert(0, str(ROOT / 'backend'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(name):
    return json.loads((PACK / name).read_text(encoding='utf8'))


def install(apply=False):
    from sqlalchemy import select
    from app.main import app  # register all models; do not create/reset tables
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.models import Tenant, User, InboxBinding, KnowledgeVersion, MaterialAsset, StoredMedia, AppSetting
    from app.operations import setting_value, save_setting, audit
    from app.reception_config import (get_reception_configuration, default_reception_configuration,
                                      ReceptionConfiguration, put_reception_configuration)
    from app.route_packages import (_validate, ROUTE_PACKAGES, install_route_package,
                                    remove_runtime_route_package)
    from app.reception_v3.skills import compile_skills

    if (settings.business_profile != 'rujing2' or settings.outbound_mode != 'disabled'
            or settings.chatwoot_write_enabled or settings.live_sop_enabled):
        raise ValueError('requires_rujing2_with_all_outbound_disabled')
    if engine.url.get_backend_name() != 'sqlite' or not engine.url.database:
        raise ValueError('requires_existing_sqlite_database')
    database = Path(engine.url.database).resolve()
    if not database.is_file():
        raise ValueError('existing_database_required_run_bootstrap_first')
    package = read('route-package.json')
    _validate(deepcopy(package), PACK / 'route-package.json')
    route = package['route_variant']
    items = read('materials.json')['items']
    identity = {name: digest(PACK / name) for name in
                ('route-package.json', 'materials.json', 'reception-config.json', 'faq.json')}
    for item in items:
        source = (PACK / 'assets' / item['filename']).resolve()
        if not source.is_relative_to((PACK / 'assets').resolve()) or digest(source) != item['sha256']:
            raise ValueError('campaign_material_hash_mismatch')
    with SessionLocal() as db:
        tenants = db.scalars(select(Tenant)).all()
        if len(tenants) != 1:
            raise ValueError('requires_exactly_one_existing_company')
        tenant = tenants[0]
        user = db.scalar(select(User).where(User.active.is_(True), User.role == 'super_admin').order_by(User.id))
        if not user:
            raise ValueError('existing_admin_required')
        if setting_value(db, 'global_message_sending', {'enabled': False})['enabled']:
            raise ValueError('global_sending_must_be_off')
        config = get_reception_configuration(db)
        if config['routing']['live_inbox_ids']:
            raise ValueError('live_inboxes_must_be_disabled')
        marker = db.get(AppSetting, MARKER)
        if marker:
            if marker.value.get('identity') != identity:
                raise ValueError('campaign_changed_use_reviewed_update_instead_of_reimport')
            if route not in ROUTE_PACKAGES or digest(Path(ROUTE_PACKAGES[route]['source_path'])) != marker.value['runtime_sha256']:
                raise ValueError('installed_route_changed_do_not_overwrite')
            for record in marker.value['assets']:
                asset = db.get(MaterialAsset, record['asset_id'])
                media = db.get(StoredMedia, record['media_id'])
                if (not asset or not media or asset.file_hash != record['sha256']
                        or asset.metadata_json.get('stored_media_id') != media.id
                        or digest(Path(media.storage_path)) != record['sha256']):
                    raise ValueError('installed_material_changed_do_not_overwrite')
            return {'status': 'already_installed', 'route': route, 'materials': len(items), 'outbound': False}
        if ROUTE_PACKAGES:
            raise ValueError('existing_routes_require_review_before_company_config_import')
        existing_assets = db.scalars(select(MaterialAsset).join(KnowledgeVersion).where(
            KnowledgeVersion.tenant_id == tenant.id,
            MaterialAsset.asset_key.in_([item['key'] for item in items]))).all()
        if existing_assets:
            raise ValueError('campaign_asset_keys_already_exist')
        patch = read('reception-config.json')
        defaults = default_reception_configuration()
        candidate = deepcopy(config)
        for section, values in patch.items():
            for key, value in values.items():
                current = config[section].get(key)
                if current != defaults[section].get(key) and current != value:
                    raise ValueError(f'operator_config_conflict:{section}.{key}')
                candidate[section][key] = value
        # The route registry is still empty during the read-only preflight.
        # Validate all other fields now; validate the route ID after installation.
        preflight_config = deepcopy(candidate)
        preflight_config['routing']['enabled_route_variants'] = []
        ReceptionConfiguration.model_validate(preflight_config)
        version = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.version_key == package['knowledge_version']))
        if version and version.tenant_id != tenant.id:
            raise ValueError('knowledge_version_company_mismatch')
        target_root = Path(settings.business_materials_dir).resolve() / 'xiamen-20261029'
        for item in items:
            target = target_root / (item['sha256'] + Path(item['filename']).suffix)
            if target.exists() and digest(target) != item['sha256']:
                raise ValueError('material_storage_collision')
        plan = {'status': 'ready', 'route': route, 'materials': len(items), 'faq': 35,
                'currency': 'TWD', 'existing_users_preserved': True, 'outbound': False}
        if not apply:
            return plan

        backup_dir = database.parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = backup_dir / f'before-xiamen-{stamp}-{uuid4().hex[:8]}.db'
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        backup.chmod(0o600)
        target_root.mkdir(parents=True, exist_ok=True)
        route_installed = False
        try:
            if version is None:
                version = KnowledgeVersion(tenant_id=tenant.id, version_key=package['knowledge_version'],
                    title=package['name'], content_hash=identity['route-package.json'],
                    source_summary={'source': 'user-doc-20261029', 'scope': 'rehearsal-only'})
                db.add(version)
                db.flush()
            records = []
            for item in items:
                target = target_root / (item['sha256'] + Path(item['filename']).suffix)
                if not target.exists():
                    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
                    shutil.copyfile(PACK / 'assets' / item['filename'], temporary)
                    if digest(temporary) != item['sha256']:
                        raise ValueError('material_copy_corrupt')
                    temporary.replace(target)
                media = StoredMedia(tenant_id=tenant.id, original_name=item['filename'],
                    media_type=item['media_type'], mime_type=mimetypes.guess_type(item['filename'])[0] or 'application/octet-stream',
                    file_size=target.stat().st_size, storage_path=str(target), created_by=user.id)
                db.add(media)
                db.flush()
                asset = MaterialAsset(knowledge_version_id=version.id, asset_key=item['key'],
                    source_path=str(target), display_name=item['display_name'], media_type=item['media_type'],
                    usage=item['usage'], file_hash=item['sha256'], available=True,
                    metadata_json={**item, 'stored_media_id': media.id, 'route_variants': [route],
                                   'content_family': item['key'], 'live_approved': False})
                db.add(asset)
                db.flush()
                records.append({'asset_id': asset.id, 'media_id': media.id, 'sha256': item['sha256']})
            inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id,
                                                         InboxBinding.channel_type == 'sandbox'))
            if inbox is None:
                used = set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.tenant_id == tenant.id)))
                external_id = -1
                while external_id in used:
                    external_id -= 1
                inbox = InboxBinding(tenant_id=tenant.id, chatwoot_inbox_id=external_id,
                                     name='廈門回覆演練', channel_type='sandbox', ai_enabled=False)
                db.add(inbox)
                db.flush()
            runtime = install_route_package(package)
            route_installed = True
            put_reception_configuration(db, ReceptionConfiguration.model_validate(candidate))
            db.flush()
            bundle = compile_skills(db)
            if set(bundle['routes']) != {route} or bundle['currency'] != 'TWD':
                raise ValueError('compiled_campaign_mismatch')
            save_setting(db, MARKER, {'identity': identity, 'runtime_sha256': digest(runtime),
                'assets': records, 'inbox_id': inbox.id, 'backup': str(backup), 'installed_at': stamp})
            audit(db, user, 'campaign.installed', 'route_package', route,
                  {'scope': 'rehearsal-only', 'materials': len(items), 'backup': str(backup)})
            db.commit()
        except Exception:
            db.rollback()
            if route_installed:
                remove_runtime_route_package(route)
            raise
        return {**plan, 'status': 'installed', 'inbox_id': inbox.id, 'backup': str(backup)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install(args.apply), ensure_ascii=False))
