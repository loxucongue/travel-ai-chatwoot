"""Dedicated release path for the second company; never reads .env.deploy.

python scripts/deploy_rujing2.py package
python scripts/deploy_rujing2.py deploy --server-note /private/server.md
python scripts/deploy_rujing2.py check --server-note /private/server.md
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output/deploy/rujing2'
HOST = '47.82.3.154'
REMOTE = '/opt/rujing2-ai'
DIRECTORIES = ('backend/app', 'backend/alembic', 'frontend/dist', 'businesses/rujing2', 'deploy/rujing2')
FILES = ('backend/pyproject.toml', 'backend/alembic.ini', 'scripts/bootstrap_rujing2.py')


def package():
    branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    if branch != 'codex/rujing2':
        raise RuntimeError('Deploy only from codex/rujing2')
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise RuntimeError('Build frontend before packaging')
    paths = [ROOT / p for p in FILES]
    paths += [p for folder in DIRECTORIES for p in (ROOT / folder).rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    for path in (ROOT / 'frontend/dist').rglob('*'):
        if path.suffix in ('.js', '.html') and re.search(r'china2go|luoxuecong|180474|128859', path.read_text(encoding='utf-8'), re.I):
            raise RuntimeError(f'Previous company found in frontend: {path.name}')
    release = datetime.now(timezone.utc).strftime('rujing2-%Y%m%dT%H%M%SZ')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = {'release': release, 'branch': branch,
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'worktree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'files': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    metadata = OUTPUT / 'manifest.json'
    metadata.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = OUTPUT / f'{release}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in paths:
            tar.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        tar.add(metadata, arcname='manifest.json')
    return archive, manifest


def connect(note):
    import paramiko
    text = Path(note).read_text(encoding='utf-8')
    if HOST not in text:
        raise RuntimeError('Server note does not match dedicated target')
    secret = re.search(r'PW:\s*(\S+)', text)
    if not secret:
        raise RuntimeError('Server password absent')
    known = OUTPUT / 'known_hosts'
    client = paramiko.SSHClient()
    if not known.exists():
        raise RuntimeError('Inspect target and record SSH host key before deployment')
    client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(HOST, username='root', password=secret[1], look_for_keys=False, allow_agent=False, timeout=20)
    return client


def run(client, command, timeout=600):
    _, out, err = client.exec_command('set -eu\n' + command, timeout=timeout)
    # Drain both streams while running so package-install output cannot fill SSH buffers.
    channel = out.channel
    chunks = []
    deadline = time.monotonic() + timeout
    while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
        if channel.recv_ready():
            value = channel.recv(65536).decode('utf-8', errors='replace')
            print(value, end='', flush=True)
            chunks.append(value)
        if channel.recv_stderr_ready():
            print(channel.recv_stderr(65536).decode('utf-8', errors='replace'), end='', flush=True)
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError('Remote command timed out')
        time.sleep(.1)
    if channel.recv_exit_status():
        raise RuntimeError('Remote command failed')
    return ''.join(chunks)


def deploy(client, archive, manifest):
    run(client, 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv nginx')
    stage = f"{REMOTE}/releases/{manifest['release']}"
    run(client, f'mkdir -p {shlex.quote(stage)} {REMOTE}/shared/data')
    with client.open_sftp() as sftp:
        sftp.put(str(archive), stage + '/release.tar.gz')
    run(client, f"cd {shlex.quote(stage)} && tar -xzf release.tar.gz && python3 -c " + shlex.quote(
        "import json,hashlib,pathlib; m=json.load(open('manifest.json')); "
        "assert all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in m['files'].items()); print('Release manifest verified')"))
    run(client, f"test -d {REMOTE}/venv || python3 -m venv {REMOTE}/venv")
    run(client, f"{REMOTE}/venv/bin/python -m pip install --disable-pip-version-check {shlex.quote(stage + '/backend')}")
    # Fixed paths and dedicated service account. Do not modify any other project.
    setup = f'''
set -eu
id rujing2 >/dev/null 2>&1 || useradd --system --home-dir {REMOTE} --shell /usr/sbin/nologin rujing2
ln -s {REMOTE}/shared/data {stage}/backend/data
'''
    run(client, setup)
    # Secrets exist only on server and in ignored credential delivery file.
    env_script = f'''
from pathlib import Path
import secrets
from cryptography.fernet import Fernet
p=Path('{REMOTE}/shared/backend.env')
if not p.exists():
    values={{'BUSINESS_PROFILE':'rujing2','COMPANY_NAME':'Rujing2','APP_ENV':'production',
    'APP_PROFILE':'evaluation','OUTBOUND_MODE':'disabled','CHATWOOT_WRITE_ENABLED':'false',
    'LIVE_SOP_ENABLED':'false','LIVE_REPLY_ACCOUNT_ID':'0','LIVE_REPLY_INBOX_ID':'0',
    'PUBLIC_API_BASE_URL':'http://{HOST}','FRONTEND_ORIGINS':'http://{HOST}',
    'APP_SECRET_KEY':secrets.token_urlsafe(48),'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),
    'DATABASE_URL':'sqlite:///{REMOTE}/shared/data/app.db',
    'UPLOAD_DIR':'{REMOTE}/shared/data/uploads','RUNTIME_PACKAGE_DIR':'{REMOTE}/shared/data/route-packages',
    'BUSINESS_MATERIALS_DIR':'{REMOTE}/shared/data/materials','SESSION_COOKIE_SECURE':'false',
    'DEEPSEEK_API_KEY':'','RELAY_BASE_URL':'','RELAY_API_TOKEN':'','LIVE_REPLY_CONCURRENCY':'2'}}
    p.write_text(''.join(k+'='+v+'\\n' for k,v in values.items()))
    p.chmod(0o640)
'''
    run(client, f'{REMOTE}/venv/bin/python -c ' + shlex.quote(env_script))
    # Read .env.rujing2 via a symlink for CLI operations, systemd uses EnvironmentFile.
    run(client, f"ln -s {REMOTE}/shared/backend.env {stage}/backend/.env.rujing2 && chown -R rujing2:rujing2 {REMOTE}/shared && chmod 750 {REMOTE}/shared")
    # The database backup is consistent even if a previous service is running.
    run(client, f"{REMOTE}/venv/bin/python -c " + shlex.quote(
        f"from pathlib import Path; import sqlite3; p=Path('{REMOTE}/shared/data/app.db'); "
        f"backup=Path('{stage}/pre-migration.db'); "
        f"p.exists() and sqlite3.connect(p).backup(sqlite3.connect(backup)); "
        f"backup.exists() and backup.chmod(0o600)"))
    run(client, f"cd {stage}/backend && runuser -u rujing2 -- {REMOTE}/venv/bin/python -m alembic upgrade head")
    run(client, f"cd {stage}/backend && if [ ! -f {REMOTE}/shared/initial-admin.json ]; then runuser -u rujing2 -- {REMOTE}/venv/bin/python ../scripts/bootstrap_rujing2.py {REMOTE}/shared/initial-admin.json; fi")
    # Verify the isolated business bundle before making it current.
    run(client, f"cd {stage}/backend && runuser -u rujing2 -- {REMOTE}/venv/bin/python -c " + shlex.quote(
        "from app.config import settings; from app.db import SessionLocal; from app.reception_v3.skills import compile_skills,SkillRegistry; "
        "assert settings.business_profile=='rujing2'; b=compile_skills(SessionLocal()); assert SkillRegistry(b).index(); print('Business preflight passed')"))
    run(client, f"test ! -L {REMOTE}/current || readlink {REMOTE}/current > {stage}/previous-release.txt\nln -s {stage} {REMOTE}/current-next\nmv -Tf {REMOTE}/current-next {REMOTE}/current")
    try:
        run(client, f"cp {stage}/deploy/rujing2/*.service /etc/systemd/system/\ncp {stage}/deploy/rujing2/nginx.conf /etc/nginx/sites-available/rujing2\nln -sf /etc/nginx/sites-available/rujing2 /etc/nginx/sites-enabled/rujing2\nnginx -t\nsystemctl daemon-reload\nsystemctl enable rujing2-api rujing2-playground\nsystemctl restart rujing2-api rujing2-playground\nsystemctl reload nginx")
        check(client)
    except Exception:
        run(client, f"if [ -s {stage}/previous-release.txt ]; then ln -sfn \"$(cat {stage}/previous-release.txt)\" {REMOTE}/current; systemctl restart rujing2-api rujing2-playground; fi")
        raise
    with client.open_sftp() as sftp:
        sftp.get(REMOTE + '/shared/initial-admin.json', str(OUTPUT / 'initial-admin.json'))
    (OUTPUT / 'deployed.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Deployment complete; login credentials saved to ignored output/deploy/rujing2/initial-admin.json')


def check(client):
    run(client, "python3 -c " + shlex.quote(
        "import time,urllib.request; time.sleep(2); "
        "r=urllib.request.urlopen('http://127.0.0.1:8012/v1/health',timeout=10); print(r.read().decode())"))
    run(client, 'systemctl is-active rujing2-api rujing2-playground nginx')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['package', 'deploy', 'check'])
    parser.add_argument('--server-note')
    args = parser.parse_args()
    if args.phase == 'package':
        archive, manifest = package()
        print(json.dumps({'archive': str(archive), 'files': len(manifest['files'])}))
        return
    if not args.server_note:
        parser.error('--server-note is required for SSH')
    client = connect(args.server_note)
    try:
        if args.phase == 'check':
            check(client)
        else:
            deploy(client, *package())
    finally:
        client.close()


if __name__ == '__main__':
    main()
