"""Server import must preserve existing company data and fail without partial routes."""
from test_business_isolation import run_isolated
import importlib.util
from pathlib import Path
import pytest


BOOTSTRAP = '''
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parent / 'scripts'))
import install_rujing2_xiamen as installer
from app.main import app
from app.db import Base, engine, SessionLocal
from app.config import settings
from app.models import Tenant, User, InboxBinding, StoredMedia, MaterialAsset, AppSetting
from app.operations import save_setting
from app.reception_config import default_reception_configuration, get_reception_configuration, SETTING_KEY
from sqlalchemy import select, func
from app.route_packages import ROUTE_PACKAGES
Base.metadata.create_all(engine)
settings.business_materials_dir = str(Path(engine.url.database).parent / 'materials')
with SessionLocal() as db:
    db.add(Tenant(id=7, name='Existing company', ai_enabled=False))
    db.add(User(id=9, email='existing@example.com', display_name='Existing administrator',
                password_hash='preserve-original-hash', role='super_admin'))
    db.flush()
    db.add(InboxBinding(id=12, tenant_id=7, chatwoot_inbox_id=88, name='Existing channel', ai_enabled=False))
    save_setting(db, SETTING_KEY, default_reception_configuration())
    save_setting(db, 'global_message_sending', {'enabled': False})
    db.commit()
'''


def test_existing_company_import_is_backed_up_and_repeatable(tmp_path):
    run_isolated(tmp_path, BOOTSTRAP + '''
assert installer.install()['status'] == 'ready'
with SessionLocal() as db:
    assert db.scalar(select(func.count(StoredMedia.id))) == 0
assert not ROUTE_PACKAGES
result = installer.install(apply=True)
assert result['status'] == 'installed'
with sqlite3.connect(result['backup']) as backup:
    assert backup.execute('select count(*) from users').fetchone()[0] == 1
    assert backup.execute('select count(*) from stored_media').fetchone()[0] == 0
with SessionLocal() as db:
    assert db.scalar(select(func.count(User.id))) == 1
    assert db.get(User, 9).password_hash == 'preserve-original-hash'
    assert db.get(Tenant, 7).name == 'Existing company'
    assert db.get(InboxBinding, 12).chatwoot_inbox_id == 88
    inbox = db.get(InboxBinding, result['inbox_id'])
    assert inbox.tenant_id == 7 and inbox.channel_type == 'sandbox' and not inbox.ai_enabled
    assert inbox.chatwoot_inbox_id < 0
    media = db.scalars(select(StoredMedia)).all()
    assert len(media) == 8 and sum(m.media_type == 'video' for m in media) == 2
    assert all(m.tenant_id == 7 and m.created_by == 9 for m in media)
    assert all(Path(m.storage_path).is_relative_to(Path(settings.business_materials_dir)) for m in media)
    assets = db.scalars(select(MaterialAsset)).all()
    assert len(assets) == 8 and all(not a.metadata_json['live_approved'] for a in assets)
    config = get_reception_configuration(db)
    assert config['routing']['enabled_route_variants'] == ['xiamen_3_6d']
    assert config['routing']['live_inbox_ids'] == []
assert installer.install(apply=True)['status'] == 'already_installed'
with SessionLocal() as db:
    assert db.scalar(select(func.count(StoredMedia.id))) == 8
    assert db.scalar(select(func.count(InboxBinding.id))) == 2
    config = get_reception_configuration(db)
    config['reply']['goal'] = 'Operator edited after import'
    save_setting(db, SETTING_KEY, config)
    db.commit()
assert installer.install(apply=True)['status'] == 'already_installed'
with SessionLocal() as db:
    assert get_reception_configuration(db)['reply']['goal'] == 'Operator edited after import'
''')


def test_import_refuses_overwriting_company_rules_or_enabled_sending(tmp_path):
    run_isolated(tmp_path, BOOTSTRAP + '''
with SessionLocal() as db:
    config = get_reception_configuration(db)
    config['reply']['goal'] = 'Company-specific rule already in use'
    save_setting(db, SETTING_KEY, config)
    db.commit()
try:
    installer.install(apply=True)
    raise AssertionError('must refuse conflicting settings')
except ValueError as error:
    assert 'operator_config_conflict:reply.goal' in str(error)
assert not ROUTE_PACKAGES
with SessionLocal() as db:
    assert db.scalar(select(func.count(StoredMedia.id))) == 0
    assert not db.get(AppSetting, installer.MARKER)
    save_setting(db, 'global_message_sending', {'enabled': True})
    db.commit()
try:
    installer.install(apply=True)
    raise AssertionError('must refuse active sending')
except ValueError as error:
    assert 'global_sending_must_be_off' in str(error)
''')


def test_failed_compile_rolls_back_database_and_route(tmp_path):
    run_isolated(tmp_path, BOOTSTRAP + '''
from unittest.mock import patch
with patch('app.reception_v3.skills.compile_skills', side_effect=RuntimeError('compile_failed')):
    try:
        installer.install(apply=True)
        raise AssertionError('must report compile failure')
    except RuntimeError as error:
        assert str(error) == 'compile_failed'
assert not ROUTE_PACKAGES
with SessionLocal() as db:
    assert db.scalar(select(func.count(StoredMedia.id))) == 0
    assert db.scalar(select(func.count(MaterialAsset.id))) == 0
    assert db.scalar(select(func.count(InboxBinding.id))) == 1
    assert not db.get(AppSetting, installer.MARKER)
    assert get_reception_configuration(db)['routing']['enabled_route_variants'] == []
assert installer.install(apply=True)['status'] == 'installed'
''')


@pytest.mark.parametrize('failure', ['preflight', 'apply'])
def test_remote_import_restart_and_preflight_boundaries(monkeypatch, failure):
    path = Path(__file__).resolve().parents[2] / 'scripts/deploy_rujing2.py'
    spec = importlib.util.spec_from_file_location('deploy_xiamen_test', path)
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    calls = []

    def run(client, command, **kwargs):
        calls.append(command)
        if command.endswith('install_rujing2_xiamen.py') and failure == 'preflight':
            raise RuntimeError('preflight_failed')
        if command.endswith('--apply') and failure == 'apply':
            raise RuntimeError('apply_failed')

    monkeypatch.setattr(deploy, 'run', run)
    with pytest.raises(RuntimeError, match=failure + '_failed'):
        deploy.install_xiamen(object())
    if failure == 'preflight':
        assert len(calls) == 1
    else:
        assert calls[1] == 'systemctl stop rujing2-api rujing2-playground'
        assert calls[-1] == 'systemctl start rujing2-api rujing2-playground'
