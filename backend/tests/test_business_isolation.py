"""Exercise real startup in fresh interpreters, without inherited company state."""
import os
from pathlib import Path
import subprocess
import sys


BACKEND = Path(__file__).resolve().parents[1]


def run_isolated(tmp_path, code):
    env = {**os.environ, 'BUSINESS_PROFILE': 'rujing2', 'APP_PROFILE': 'evaluation',
           'OUTBOUND_MODE': 'disabled', 'CHATWOOT_WRITE_ENABLED': 'false', 'LIVE_SOP_ENABLED': 'false',
           'LIVE_REPLY_ACCOUNT_ID': '0', 'LIVE_REPLY_INBOX_ID': '0',
           'DATABASE_URL': f'sqlite:///{(tmp_path / "app.db").as_posix()}',
           'RUNTIME_PACKAGE_DIR': str(tmp_path / 'routes'), 'UPLOAD_DIR': str(tmp_path / 'uploads'),
           'DEEPSEEK_API_KEY': '', 'RELAY_BASE_URL': '', 'RELAY_API_TOKEN': ''}
    result = subprocess.run([sys.executable, '-c', code], cwd=BACKEND, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_company_migrates_and_management_endpoints_work(tmp_path):
    run_isolated(tmp_path, '''
import json
from alembic.config import Config
from alembic import command
command.upgrade(Config('alembic.ini'), 'head')
from app.main import app
from app.config import settings
from app.db import SessionLocal
from app.models import Tenant, User, InboxBinding
from app.security import hash_password
from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NODES, RUNTIME_PACKAGE_ROOT
from app.reception_v3.skills import compile_skills, SkillRegistry
from app.reception_v3.prompt import SYSTEM_PROMPT
from fastapi.testclient import TestClient
assert not ROUTES and not UNCLASSIFIED_SOP_NODES
from app.business_profile import PROFILE
assert PROFILE['currency'] == 'TWD'
assert 'rujing2' in settings.database_url or settings.database_url.endswith('app.db')
assert not settings.outbound_enabled and not settings.deepseek_api_key
assert not settings.reception_repeat_review_enabled
with SessionLocal() as db:
    db.add(Tenant(name='Test Company'))
    db.add(User(email='admin@example.com', password_hash=hash_password('test-password'), display_name='Admin'))
    db.flush()
    db.add(InboxBinding(tenant_id=1, chatwoot_inbox_id=1, name='Isolated inbox'))
    db.commit()
    bundle = compile_skills(db)
assert bundle['routes'] == {} and bundle['service_knowledge']['facts'] == []
assert not bundle['silence']['enabled'] and not bundle['lead_capture']['enabled']
registry = SkillRegistry(bundle)
assert [s['name'] for s in registry.index()] == ['business-reception']
content = json.dumps(bundle, ensure_ascii=False) + SYSTEM_PROMPT + registry.load('business-reception')['instructions']
assert all(word not in content for word in ['China2Go','china2go','桃花','西藏','珠峰','luoxuecong'])
with TestClient(app) as client:
    assert client.get('/v1/health').status_code == 200
    assert client.get('/v1/automation/route-products').status_code == 401
    login = client.post('/v1/auth/login', json={'email':'admin@example.com','password':'test-password'})
    assert login.status_code == 200, login.text
    for path in ['/v1/automation/route-products','/v1/automation/reception-config','/v1/materials?inbox_binding_id=1',
                 '/v1/knowledge/web-sources','/v1/settings/chatwoot','/v1/bi/overview']:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert 'china2go' not in response.text.lower()
    config = client.get('/v1/automation/reception-config').json()['config']
    assert config['routing']['enabled_route_variants'] == []
    assert not config['lead_capture']['next_day_invitation_enabled']
    channels = client.get('/v1/settings/channel-ai')
    assert channels.status_code == 200
    assert not channels.json()['global_sending_enabled']
    enabled = client.patch('/v1/settings/channel-ai', json={
        'revision': channels.json()['revision'], 'changes':[{'id':1, 'enabled':True}]},
        headers={'X-CSRF-Token':login.json()['csrf_token']})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['items'][0]['enabled']
    assert not enabled.json()['global_sending_enabled']
    preserved = client.get('/v1/automation/reception-config').json()['config']
    assert preserved['routing']['live_inbox_ids'] == [1]
    assert preserved['routing']['enabled_route_variants'] == []
    assert not preserved['lead_capture']['enabled'] and not preserved['silence']['enabled']
    response = client.patch('/v1/automation/reception-config', json={'reply': {'goal':'Answer approved facts'}},
                            headers={'X-CSRF-Token':login.json()['csrf_token']})
    assert response.status_code == 200, response.text
''')


def test_first_product_can_be_published_without_legacy_catalog_or_skill(tmp_path):
    run_isolated(tmp_path, '''
from app.db import Base, engine, SessionLocal
from app.main import app
from app.models import Tenant, User
from app.security import hash_password
from fastapi.testclient import TestClient
from app.route_packages import install_route_package, ROUTE_PACKAGES
from app.reception_config import ReceptionConfiguration, put_reception_configuration, default_reception_configuration
from app.reception_v3.skills import compile_skills, SkillRegistry
from app.reception_v3 import release_id
Base.metadata.create_all(engine)
package = {'schema_version':1, 'route_variant':'sample_product', 'package_version':'test-v1',
    'knowledge_version':'rujing2-v1', 'branch':'sample', 'name':'Sample product',
    'selection_title':'Sample product', 'default_entry_message':'Tell me about this product',
    'required_slots':['interest'], 'knowledge_facts':[{'id':'sample.fact','text':'A reviewed fact','source_ref':'operator'}],
    'content_groups':{'intro':{'approved_text':'A reviewed introduction','initial_delivery':True,
        'delivery_mode':'text_only','asset_keys':[],'evidence_refs':['sample.fact']}}, 'content_sequence':['intro']}
with SessionLocal() as db:
    db.add(Tenant(name='Sample company'))
    db.add(User(email='admin@example.com', display_name='Admin', password_hash=hash_password('test-password')))
    db.commit()
with TestClient(app) as client:
    login = client.post('/v1/auth/login', json={'email':'admin@example.com','password':'test-password'})
    response = client.post('/v1/automation/route-products/import', json={'package': package},
                           headers={'X-CSRF-Token':login.json()['csrf_token']})
    assert response.status_code == 200, response.text
assert list(ROUTE_PACKAGES) == ['sample_product']
with TestClient(app) as client:
    client.post('/v1/auth/login', json={'email':'admin@example.com','password':'test-password'})
    product = client.get('/v1/automation/route-products').json()['items'][0]
    assert product['currency'] == 'TWD'
with SessionLocal() as db:
    config = default_reception_configuration()
    config['routing']['enabled_route_variants'] = ['sample_product']
    put_reception_configuration(db, ReceptionConfiguration.model_validate(config))
    db.commit()
    bundle = compile_skills(db)
    registry = SkillRegistry(bundle)
    assert bundle['currency'] == bundle['routes']['sample_product']['currency'] == 'TWD'
    assert registry.route_skill('sample_product') == 'sample-product'
    skill = registry.load('sample-product')
    assert 'A reviewed fact' in skill['instructions'] and 'A reviewed introduction' in skill['instructions']
    assert len(bundle['routes']) == 1
''')


def test_deployment_manifest_excludes_original_company_and_private_files(tmp_path):
    import importlib.util
    path = BACKEND.parent / 'scripts/deploy_rujing2.py'
    spec = importlib.util.spec_from_file_location('deployment', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert 'businesses/rujing2' in module.DIRECTORIES
    assert 'businesses/china2go' not in module.DIRECTORIES
    assert not any(p.startswith(('backend/data', 'docs', 'backend/scripts')) for p in module.DIRECTORIES)
