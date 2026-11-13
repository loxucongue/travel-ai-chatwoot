"""Read production sources into an isolated local test tree; no remote writes."""
import hashlib
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/beijing-acceptance'


def main():
    sys.path.insert(0, 'E:/ai_code/travel_ai_Chatwoot/output/new-routes-20261014')
    from deploy_inspect import connect
    import paramiko
    original = paramiko.SSHClient.connect
    paramiko.SSHClient.connect = lambda self, *a, **kw: original(self, *a, **dict(kw, allow_agent=False, look_for_keys=False, auth_timeout=15, banner_timeout=15))
    baseline = json.loads((OUT / 'baseline.json').read_text(encoding='utf8'))
    stage = OUT / 'stage'
    with connect() as client, client.open_sftp() as sftp:
        _, stdout, stderr = client.exec_command('cd /opt/china2go-ai && tar czf - --exclude=__pycache__ backend/app data/knowledge/china2go/route-packages data/knowledge/china2go/service-knowledge', timeout=120)
        payload = stdout.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            archive.extractall(stage, filter='data')
        for relative, expected in baseline['files'].items():
            if not relative.startswith(('backend/app/', 'data/knowledge/')):
                continue
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                sftp.get('/opt/china2go-ai/' + relative, str(target))
            assert hashlib.sha256(target.read_bytes()).hexdigest() == expected['sha256'], relative
        # Credentials stay in ignored acceptance output, never logs or artifacts.
        sftp.get('/opt/china2go-ai/backend/.env', str(stage / 'backend/.env'))
    for relative, package in baseline['packages'].items():
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(package, ensure_ascii=False), encoding='utf8')
    shutil.copytree(ROOT / 'backend/app/reception_v3/skills/beijing-micro-5d', stage / 'backend/app/reception_v3/skills/beijing-micro-5d', dirs_exist_ok=True)
    target = stage / 'backend/data/route-packages/beijing-micro-5d/route-package.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing/route-package.json', target)
    print(json.dumps({'staged': True, 'production_modified': False}))


if __name__ == '__main__':
    main()
