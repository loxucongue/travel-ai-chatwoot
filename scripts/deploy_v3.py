"""Full code release, preserving live configuration/data and historical jobs.

Local: .venv/Scripts/python scripts/deploy_v3.py stage|deploy|check|rollback
The remote phases run with the server's existing Python environment. No model or
customer-channel request is made by preflight. Rollback never restores the DB.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone

SERVICES = ['china2go-worker', 'china2go-api', 'china2go-playground']
LIVE = Path('/opt/china2go-ai')
RELEASE_PATHS = ('backend/app', 'backend/alembic', 'backend/scripts', 'scripts', 'frontend/dist',
                 'data/knowledge/china2go/route-packages', 'data/knowledge/china2go/service-knowledge')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    with sqlite3.connect(root / 'backend/data/app.db') as db:
        settings = dict(db.execute('select key,value from app_settings'))
        return {'settings_hash': hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
                'sending': json.loads(settings.get('global_message_sending', '{}')),
                'outbound': db.execute('select count(*) from outbound_messages').fetchone()[0],
                'jobs': db.execute('select status,count(*) from live_reply_jobs group by status').fetchall(),
                'engines': db.execute('select ai_engine_version,ai_engine_release_id,count(*) from conversation_states group by 1,2').fetchall(),
                'migration': db.execute('select version_num from alembic_version').fetchall(),
                'env_hash': digest(root / 'backend/.env')}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def require_disk_space(required_bytes):
    free = shutil.disk_usage(LIVE).free
    if free < required_bytes:
        raise RuntimeError(f'insufficient_disk_space:required={required_bytes}:free={free}')


def preflight(stage):
    # Use a copied DB and existing configuration; no outbound transport allowed.
    require_disk_space((LIVE / 'backend/data/app.db').stat().st_size + 1024 ** 3)
    (stage / 'backend/data').mkdir(parents=True, exist_ok=True)
    # Schema/configuration checks only need runtime route overrides and a DB.
    # Uploaded media and backup trees are not preflight inputs.
    overrides = LIVE / 'backend/data/route-packages'
    if overrides.exists():
        shutil.copytree(overrides, stage / 'backend/data/route-packages', dirs_exist_ok=True)
    with sqlite3.connect(LIVE / 'backend/data/app.db') as source, sqlite3.connect(stage / 'backend/data/app.db') as target:
        source.backup(target)
    shutil.copy2(LIVE / 'backend/.env', stage / 'backend/.env')
    shutil.copy2(LIVE / 'backend/alembic.ini', stage / 'backend/alembic.ini')
    # Keep candidate knowledge files; copy only live-only supporting documents.
    for source in (LIVE / 'data/knowledge').rglob('*'):
        if source.is_file():
            target = stage / 'data/knowledge' / source.relative_to(LIVE / 'data/knowledge')
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    env = {**os.environ, 'APP_PROFILE': 'evaluation', 'OUTBOUND_MODE': 'disabled',
           'CHATWOOT_WRITE_ENABLED': 'false', 'LIVE_SOP_ENABLED': 'false',
           'DATABASE_URL': 'sqlite:///' + str(stage / 'backend/data/app.db')}
    code = '''import socket,json,importlib.metadata,tomllib
try:
 from packaging.requirements import Requirement
except ImportError:
 from pip._vendor.packaging.requirements import Requirement
from pathlib import Path
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('preflight_network_disabled'))
deps=tomllib.loads(Path('pyproject.toml').read_text())['project']['dependencies']
incompatible=[]
for item in deps:
 r=Requirement(item)
 try: v=importlib.metadata.version(r.name)
 except importlib.metadata.PackageNotFoundError: v=None
 if v is None or not r.specifier.contains(v): incompatible.append({'name':r.name,'installed':v,'required':str(r.specifier)})
from app.main import app
from app.config import settings
from app.reception_v3 import release_id
ENGINE_RELEASE_ID=release_id()
from alembic.config import Config
from alembic import command
command.upgrade(Config("alembic.ini"), "head")
assert not settings.outbound_enabled
from sqlalchemy import create_engine,inspect
from app.db import Base
from app import models,automation_models,live_reply_models,lead_capture_models
ins=inspect(create_engine(settings.database_url))
missing={}
for name,table in Base.metadata.tables.items():
 columns={c['name'] for c in ins.get_columns(name)} if ins.has_table(name) else set()
 if set(table.columns.keys())-columns: missing[name]=sorted(set(table.columns.keys())-columns)
assert not missing, missing
from app.db import SessionLocal
from app.reception_config import get_reception_configuration, live_silence_enabled
from app.reception_v3.skills import compile_skills
with SessionLocal() as db:
 config = get_reception_configuration(db)
 live_silence_enabled(db)
 assert config['reply']['opening_items'] or config['reply']['opening_messages']
 bundle=compile_skills(db)
print(json.dumps({'release':ENGINE_RELEASE_ID,'schema_compatible':True,'dependency_mismatches':incompatible,'outbound':False,
                 'skill_digest':bundle['digest'],'service_version':bundle['service_knowledge']['version']}))
'''
    try:
        result = subprocess.run([str(LIVE / 'venv/bin/python'), '-c', code], cwd=stage / 'backend', env=env,
                                capture_output=True, text=True)
    finally:
        # Preflight data is an expendable copy, not the release rollback backup.
        copied_data = stage / 'backend/data'
        assert copied_data.resolve().is_relative_to((LIVE / 'releases').resolve())
        shutil.rmtree(copied_data)
    if result.returncode:
        print(result.stderr)
        raise RuntimeError('isolated_preflight_failed')
    data = json.loads(result.stdout.strip().splitlines()[-1])
    write(stage / 'preflight.json', data)
    assert not data['dependency_mismatches'], data['dependency_mismatches']
    return data


def quiesce():
    # A SQLite write lock prevents any new job claim while stopping the worker.
    # Wait for current deliveries; never interrupt an active submission.
    deadline = time.monotonic() + 120
    while True:
        with sqlite3.connect(LIVE / 'backend/data/app.db', timeout=20) as db:
            db.execute('begin immediate')
            busy = db.execute("select count(*) from live_reply_jobs where status='processing'").fetchone()[0]
            busy += db.execute("select count(*) from live_sop_jobs where status='processing'").fetchone()[0]
            busy += db.execute("select count(*) from automation_runs where status='processing' and session_id in (select id from automation_sessions where environment='live')").fetchone()[0]
            busy += db.execute("select count(*) from automation_sessions, json_each(automation_sessions.messages) where environment='live' and json_extract(json_each.value,'$.status')='submitting'").fetchone()[0]
            if not busy:
                subprocess.run(['systemctl', 'stop', SERVICES[0]], check=True, timeout=40)
                break
        if time.monotonic() > deadline:
            raise RuntimeError('active_delivery_did_not_drain')
        time.sleep(1)
    subprocess.run(['systemctl', 'stop', *SERVICES[1:]], check=True, timeout=40)


def health():
    import urllib.request
    for attempt in range(25):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=2) as response:
                assert response.status == 200
            break
        except Exception:
            if attempt == 24:
                raise
            time.sleep(1)
    subprocess.run(['systemctl', 'is-active', *SERVICES], check=True)


def restore(backup):
    quiesce()
    for rel in RELEASE_PATHS:
        target = LIVE / rel
        # Fixed directories under the explicitly named deployment root only.
        assert target.resolve().is_relative_to(LIVE.resolve())
        shutil.rmtree(target)
        shutil.copytree(backup / rel, target)
    if (backup / 'backend/pyproject.toml').exists():
        shutil.copy2(backup / 'backend/pyproject.toml', LIVE / 'backend/pyproject.toml')
    bindings = json.loads((backup / 'bindings.json').read_text())
    with sqlite3.connect(LIVE / 'backend/data/app.db') as db:
        for row in bindings:
            db.execute('update conversation_states set ai_engine_version=?,ai_engine_release_id=? where id=?',
                       (row[1], row[2], row[0]))
    subprocess.run(['systemctl', 'start', *SERVICES[1:], SERVICES[0]], check=True)
    health()


def remote_phase(phase, stage):
    assert stage.parent == LIVE / 'releases' and stage.name.startswith('v3-consolidation-')
    backup = LIVE / 'backups' / stage.name
    if phase == 'stage':
        return preflight(stage)
    if phase == 'rollback':
        restore(backup)
        return {'rolled_back': True, **inventory(LIVE)}
    if phase == 'check':
        health()
        return inventory(LIVE)
    manifest = json.loads((stage / 'manifest.json').read_text())
    for rel, expected in manifest['files'].items():
        assert digest(stage / rel) == expected, rel
    checked = json.loads((stage / 'preflight.json').read_text())
    assert not checked['dependency_mismatches'] and checked['schema_compatible']
    before = inventory(LIVE)
    backup_bytes = (LIVE / 'backend/data/app.db').stat().st_size
    backup_bytes += sum(p.stat().st_size for rel in RELEASE_PATHS
                        for p in (LIVE / rel).rglob('*') if p.is_file())
    require_disk_space(backup_bytes + 1024 ** 3)
    backup.mkdir(parents=True, exist_ok=False)
    swapped = False
    try:
        quiesce()
        before = inventory(LIVE)
        with sqlite3.connect(LIVE / 'backend/data/app.db') as source, sqlite3.connect(backup / 'app.db') as target:
            source.backup(target)
            write(backup / 'bindings.json', source.execute('select id,ai_engine_version,ai_engine_release_id from conversation_states').fetchall())
        shutil.copy2(LIVE / 'backend/.env', backup / 'backend.env')
        for rel in RELEASE_PATHS:
            shutil.copytree(LIVE / rel, backup / rel)
        shutil.copy2(LIVE / 'backend/pyproject.toml', backup / 'backend/pyproject.toml')
        write(backup / 'before.json', before)
        for rel in RELEASE_PATHS:
            target = LIVE / rel
            assert target.resolve().is_relative_to(LIVE.resolve())
            swapped = True
            shutil.rmtree(target)
            shutil.copytree(stage / rel, target)
        shutil.copy2(stage / 'backend/pyproject.toml', LIVE / 'backend/pyproject.toml')
        # Migrate schema and retire old jobs without replaying historical input.
        subprocess.run([str(LIVE / 'venv/bin/python'), '-m', 'alembic', 'upgrade', 'head'], cwd=LIVE / 'backend', check=True)
        with sqlite3.connect(LIVE / 'backend/data/app.db') as db:
            db.execute("update conversation_states set ai_engine_release_id=? where ai_engine_version='v3'", (checked['release'],))
        for rel, expected in manifest['files'].items():
            if any(rel.startswith(folder + '/') for folder in RELEASE_PATHS):
                assert digest(LIVE / rel) == expected, rel
        subprocess.run(['systemctl', 'start', *SERVICES[1:], SERVICES[0]], check=True)
        health()
        after = inventory(LIVE)
        assert before['settings_hash'] == after['settings_hash'] and before['env_hash'] == after['env_hash']
        result = {'release': stage.name, 'engine_release': checked['release'], 'backup': str(backup),
                  'before': before, 'after': after, 'git_commit': manifest['git_commit']}
        write(stage / 'deployment.json', result)
        write(LIVE / 'current-release.json', result)
        return result
    except Exception:
        if swapped:
            restore(backup)
        else:
            subprocess.run(['systemctl', 'start', *SERVICES[1:], SERVICES[0]], check=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['stage', 'deploy', 'check', 'rollback'])
    parser.add_argument('--remote-stage')
    args = parser.parse_args()
    if args.remote_stage:
        print(json.dumps(remote_phase(args.phase, Path(args.remote_stage)), ensure_ascii=False))
        return
    import paramiko
    import shlex
    root = Path(__file__).resolve().parents[1]
    out = root / 'output/v3-consolidation-release'
    out.mkdir(parents=True, exist_ok=True)
    config = {}
    for line in (root / '.env.deploy').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1); config[k.strip()] = v.strip().strip('"').strip("'")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(config['DEPLOY_HOST'], username=config['DEPLOY_SSH_USER'], password=config['DEPLOY_SSH_PASSWORD'], timeout=20)
    try:
        if args.phase == 'stage':
            name = 'v3-consolidation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            stage = (LIVE / 'releases' / name).as_posix()
            # Ship tracked source only; local analysis scripts never enter releases.
            tracked = subprocess.check_output(['git', 'ls-files'], cwd=root, text=True).splitlines()
            files = [root / name for name in tracked
                     if any(name.startswith(rel + '/') for rel in RELEASE_PATHS)
                     and (root / name).is_file()]
            files += [p for p in (root / 'frontend/dist').rglob('*') if p.is_file()]
            files += [root / 'backend/pyproject.toml']
            manifest = {'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                        'files': {p.relative_to(root).as_posix(): digest(p) for p in files}}
            write(out / 'manifest.json', manifest)
            archive = out / 'candidate.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for p in files:
                    tar.add(p, arcname=p.relative_to(root).as_posix())
                tar.add(out / 'manifest.json', arcname='manifest.json')
                tar.add(Path(__file__), arcname='release.py')
            with client.open_sftp() as sftp:
                sftp.put(str(archive), '/tmp/' + name + '.tar.gz')
            command = 'mkdir -p ' + shlex.quote(stage) + '\ntar -xzf /tmp/' + name + '.tar.gz -C ' + shlex.quote(stage)
            _, stdout, stderr = client.exec_command(command)
            assert stdout.channel.recv_exit_status() == 0, stderr.read().decode()
            with client.open_sftp() as sftp:
                sftp.remove('/tmp/' + name + '.tar.gz')
            write(out / 'stage.json', {'stage': stage})
        else:
            stage = json.loads((out / 'stage.json').read_text())['stage']
        command = '/opt/china2go-ai/venv/bin/python ' + shlex.quote(stage + '/release.py') + ' ' + args.phase + ' --remote-stage ' + shlex.quote(stage)
        _, stdout, stderr = client.exec_command(command, timeout=300)
        output, error = stdout.read().decode(), stderr.read().decode()
        status = stdout.channel.recv_exit_status()
        (out / (args.phase + '.log')).write_text(output + '\n' + error, encoding='utf8')
        print(output)
        if status:
            print(error)
            raise SystemExit(status)
        write(out / (args.phase + '-result.json'), json.loads(output.strip().splitlines()[-1]))
    finally:
        client.close()


if __name__ == '__main__':
    main()
