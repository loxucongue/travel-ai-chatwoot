"""Run on China2Go under deploy.lock after exact release upload."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = Path('/opt/china2go-ai')


def main():
    upload = Path(sys.argv[1]).resolve()
    baseline = json.loads((upload / 'baseline.json').read_text())
    release_id = 'beijing-micro-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lock = (ROOT / 'deploy.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for relative, evidence in baseline['files'].items():
        if relative.startswith('backend/app/'):
            path = ROOT / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']:
                raise RuntimeError('production_baseline_changed:' + relative)
    with sqlite3.connect('file:' + str(ROOT / 'backend/data/app.db') + '?mode=ro', uri=True) as conn:
        current = conn.execute("select value from app_settings where key='route_reception_config'").fetchone()[0]
    if hashlib.sha256(current.encode()).hexdigest() != baseline['setting_hashes']['route_reception_config']:
        raise RuntimeError('production_configuration_changed')
    skill = ROOT / 'backend/app/reception_v3/skills/beijing-micro-5d/SKILL.md'
    package_path = ROOT / 'backend/data/route-packages/beijing_micro_5d/route-package.json'
    release = ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing'
    if skill.exists() or package_path.exists() or release.exists():
        raise RuntimeError('beijing_already_installed_review_existing_release')
    backup = ROOT / 'backups' / release_id
    backup.mkdir(parents=True)
    (backup / 'route_reception_config.json').write_text(current)
    (backup / 'scope.json').write_text(json.dumps({'release': release_id,
        'new_route': 'beijing_micro_5d', 'new_skill': str(skill), 'new_release': str(release),
        'database_scope': 'add materials/media and update route_reception_config only',
        'historical_messages_modified': False}))
    shutil.copytree(upload / 'release', release)
    skill.parent.mkdir(parents=True)
    shutil.copyfile(upload / 'SKILL.md', skill)
    os.chdir(ROOT / 'backend')
    sys.path.insert(0, str(ROOT / 'backend'))
    sys.path.insert(0, str(upload))
    from app.db import SessionLocal
    from app.models import MaterialAsset, StoredMedia
    from app.material_library import candidate_materials, resolve_materials
    from app.reception_v3.skills import compile_skills, SkillRegistry
    from install_beijing_package import install
    from sqlalchemy import select
    with SessionLocal() as db:
        try:
            result = install(db, release, tenant_id=1, user_id=1, activate=True)
            bundle = compile_skills(db)
            registry = SkillRegistry(bundle, candidate_materials(db, 1))
            assert registry.route_skill('beijing_micro_5d') == 'beijing-micro-5d'
            manifest = json.loads((release / 'asset-manifest.json').read_text())
            for asset in manifest:
                assert resolve_materials(db, [asset['asset_key']], 'beijing_micro_5d', 1)
            records = [{'asset_id': row.id, 'media_id': row.metadata_json['stored_media_id'], 'key': row.asset_key}
                       for row in db.scalars(select(MaterialAsset).where(MaterialAsset.asset_key.in_([m['asset_key'] for m in manifest])))]
            assert len(records) == 40
            (backup / 'new_records.json').write_text(json.dumps(records))
            db.commit()
        except Exception:
            db.rollback()
            # Newly introduced files only; retain release assets for diagnosis.
            package_path.unlink(missing_ok=True)
            skill.unlink(missing_ok=True)
            raise
    receipt = {**result, 'release_id': release_id, 'backup': str(backup), 'resolved_assets': 40,
               'existing_runtime_unchanged': True, 'channel_configuration_unchanged': True,
               'history_replayed': False}
    (backup / 'receipt.json').write_text(json.dumps(receipt))
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
