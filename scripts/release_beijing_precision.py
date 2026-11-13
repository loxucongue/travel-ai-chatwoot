"""Deploy exact tested files against a live baseline, preserving other patches."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = Path('/opt/china2go-ai')


def main():
    upload = Path(sys.argv[1]).resolve()
    baseline = json.loads((upload / 'baseline.json').read_text())
    manifest = json.loads((upload / 'patch-manifest.json').read_text())
    services = ['china2go-api', 'china2go-worker', 'china2go-playground']
    with (ROOT / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name, expected in baseline['files'].items():
            if name.startswith('backend/app/'):
                assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected['sha256'], name
        for name, expected in baseline['packages'].items():
            assert json.loads((ROOT / name).read_text()) == expected, name
        with sqlite3.connect('file:' + str(ROOT / 'backend/data/app.db') + '?mode=ro', uri=True) as db:
            setting = db.execute("select value from app_settings where key='route_reception_config'").fetchone()[0]
            assert hashlib.sha256(setting.encode()).hexdigest() == baseline['setting_hashes']['route_reception_config']
        for name, expected in manifest.items():
            raw = (upload / 'files' / name).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == expected
            if name.endswith('.py'):
                compile(raw, name, 'exec')
        release_id = 'beijing-precision-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = ROOT / 'backups' / release_id
        release = ROOT / 'releases' / release_id
        backup.mkdir(); release.mkdir()
        for name in [*manifest, 'current-release.json']:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        changed = []
        try:
            for name in manifest:
                target = ROOT / name
                tmp = target.with_name(target.name + '.beijing-new')
                shutil.copy2(upload / 'files' / name, tmp)
                shutil.copymode(target, tmp)
                os.replace(tmp, target)
                changed.append(name)
                copy = release / name
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, copy)
            subprocess.run(['systemctl', 'restart', *services], check=True, timeout=90)
            health = None
            for _ in range(20):
                try:
                    health = json.loads(urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=4).read())
                    if health.get('status') == 'ok':
                        break
                except Exception:
                    pass
                time.sleep(1)
            assert health and health.get('status') == 'ok'
            active = subprocess.run(['systemctl', 'is-active', *services], capture_output=True, text=True).stdout.splitlines()
            assert active == ['active'] * 3
            os.chdir(ROOT / 'backend'); sys.path.insert(0, str(ROOT / 'backend'))
            from app.reception_v3 import release_id as engine_release
            from app.reception_v3.skills import compile_skills
            from app.db import SessionLocal
            with SessionLocal() as db:
                bundle = compile_skills(db)
                assert bundle['routes']['beijing_micro_5d']['version'] == '2026-11-13.beijing-micro-v2'
            receipt = {'release': release_id, 'backup': str(backup), 'engine': engine_release(),
                       'at': datetime.now(timezone.utc).isoformat(), 'healthy': True, 'services': active,
                       'patch_files': list(manifest), 'sha256': manifest, 'customer_test_sends': 0,
                       'history_replayed': False, 'database_configuration_changed': False}
            (release / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
            (backup / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
            temp = ROOT / 'current-release.json.new'
            temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2));os.replace(temp, ROOT / 'current-release.json')
            print(json.dumps(receipt))
        except Exception:
            for name in changed:
                shutil.copy2(backup / name, ROOT / name)
            subprocess.run(['systemctl', 'restart', *services], timeout=90)
            raise


if __name__ == '__main__':
    main()
