"""Publish the reviewed security fixes without changing AI policy or send scope."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tarfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "output/deploy"))
from deploy_products import Remote, read_env
from deploy_advisor_feedback import BASELINE

OUT = ROOT / "output/review-fixes-20260916"
APP_NAMES = {"api.py", "auth.py", "models.py", "ops_api.py"}
SERVICES = "china2go-api china2go-worker china2go-playground"
MIGRATION = "backend/alembic/versions/d2f6a8b901ce_review_security_consistency.py"


def guards(remote):
    return json.loads(remote.run(BASELINE, label="protected production state").strip().splitlines()[-1])


def remote_python(remote, code, label):
    return remote.run("cd /opt/china2go-ai && venv/bin/python - <<'PY'\n" + code + "\nPY", label=label)


def stage(remote):
    before = guards(remote)
    assert before["sending"] is False, "sending_must_remain_disabled"
    baseline = json.loads((OUT / "production-app-hashes.json").read_text())
    changes = {name for name, digest in baseline.items()
               if hashlib.sha256((ROOT / "backend/app" / name).read_bytes()).hexdigest() != digest}
    assert changes == APP_NAMES, f"unexpected_backend_changes:{changes}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = "review-fixes-" + stamp
    path = "/opt/china2go-ai/releases/" + release
    names = ["backend/app/" + name for name in sorted(APP_NAMES)] + [
        MIGRATION, "backend/pyproject.toml", "backend/tests/test_review_security.py",
        "backend/tests/test_review_migration.py", "frontend/src/auth.tsx",
        "frontend/src/main.tsx", "frontend/src/session-cache.ts", "frontend/src/pages/Handoff.tsx",
        "frontend/package.json", "frontend/tests/session-cache.test.mjs", "README.md",
        "docs/development/project-review-fixes-20260916.md",
        "scripts/deploy_review_fixes_20260916.py",
    ]
    names += [p.relative_to(ROOT).as_posix() for p in (ROOT / "frontend/dist").rglob("*") if p.is_file()]
    manifest = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    archive = OUT / (release + ".tar.gz")
    with tarfile.open(archive, "w:gz") as bundle:
        for name in names:
            bundle.add(ROOT / name, arcname=name)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    upload = "/tmp/" + archive.name
    with remote.client.open_sftp() as sftp:
        sftp.put(str(archive), upload)
    remote_python(remote, f'''
import hashlib,json,tarfile,shutil,sqlite3
from pathlib import Path
root=Path('/opt/china2go-ai')
stage=Path({path!r})
assert hashlib.sha256(Path({upload!r}).read_bytes()).hexdigest()=={archive_hash!r}
stage.mkdir()
manifest=json.loads({json.dumps(manifest)!r})
with tarfile.open({upload!r}) as bundle:
 for member in bundle.getmembers():
  assert member.isfile() and member.name in manifest
 bundle.extractall(stage)
for name,digest in manifest.items():
 assert hashlib.sha256((stage/name).read_bytes()).hexdigest()==digest,name
baseline=json.loads({json.dumps(baseline)!r})
for name,digest in baseline.items():
 assert hashlib.sha256((root/'backend/app'/name).read_bytes()).hexdigest()==digest,'production_changed:'+name
check=stage/'migration-check/backend'
shutil.copytree(root/'backend/app',check/'app',ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(root/'backend/alembic',check/'alembic',ignore=shutil.ignore_patterns('__pycache__'))
for name in {sorted(APP_NAMES)!r}:shutil.copy2(stage/'backend/app'/name,check/'app'/name)
shutil.copy2(stage/{MIGRATION!r},check/'alembic/versions'/Path({MIGRATION!r}).name)
shutil.copy2(root/'backend/alembic.ini',check/'alembic.ini')
shutil.copy2(root/'backend/.env',check/'.env');(check/'.env').chmod(0o600)
(check/'data').mkdir()
with sqlite3.connect(root/'backend/data/app.db') as source,sqlite3.connect(check/'data/app.db') as target:source.backup(target)
(stage/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps({{'release':{release!r},'files':len(manifest)}}))
''', "verify release and isolate migration rehearsal")
    remote.run(f"cd {shlex.quote(path)}/migration-check/backend\n"
               "export APP_PROFILE=evaluation OUTBOUND_MODE=disabled CHATWOOT_WRITE_ENABLED=false LIVE_SOP_ENABLED=false\n"
               f"export DATABASE_URL=sqlite:///{path}/migration-check/backend/data/app.db\n"
               "set -e\n/opt/china2go-ai/venv/bin/python -m alembic upgrade head\n"
               "/opt/china2go-ai/venv/bin/python -m alembic check", label="rehearse production-snapshot migration")
    info = {"release": release, "stage": path, "archive": str(archive), "archive_sha256": archive_hash,
            "manifest": manifest, "baseline_app": baseline, "before": before,
            "backup": "/opt/china2go-ai/backups/" + release}
    (OUT / "stage.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps({"staged": release, "migration_rehearsal": "passed"}))


def deploy(remote):
    info = json.loads((OUT / "stage.json").read_text())
    assert guards(remote) == info["before"], "production_settings_changed"
    for name, digest in info["manifest"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, "local_source_changed:" + name
    path, backup = info["stage"], info["backup"]
    remote_python(remote, f'''
import hashlib,json
from pathlib import Path
for name,digest in json.loads({json.dumps(info['baseline_app'])!r}).items():
 assert hashlib.sha256((Path('backend/app')/name).read_bytes()).hexdigest()==digest,name
''', "check unchanged production source")
    stopped = backup_ready = installed = started = False
    try:
        remote.run(f"systemctl stop {SERVICES}", label="pause services for migration")
        stopped = True
        assert guards(remote) == info["before"]
        remote_python(remote, f'''
import json,shutil,sqlite3,tarfile
from pathlib import Path
root=Path('/opt/china2go-ai');backup=Path({backup!r});backup.mkdir()
manifest=json.loads({json.dumps(info['manifest'])!r})
existing=[name for name in manifest if (root/name).is_file()]
with tarfile.open(backup/'code.tar.gz','w:gz') as archive:
 for name in existing:archive.add(root/name,arcname=name)
(backup/'new-files.json').write_text(json.dumps([name for name in manifest if name not in existing]))
shutil.copy2(root/'backend/.env',backup/'backend.env');(backup/'backend.env').chmod(0o600)
with sqlite3.connect(root/'backend/data/app.db') as src,sqlite3.connect(backup/'app.db') as dst:src.backup(dst)
with sqlite3.connect(backup/'app.db') as db:assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
print('database_and_code_backup_verified')
''', "backup production before schema change")
        backup_ready = True
        installed = True
        remote_python(remote, f'''
import json,hashlib,shutil
from pathlib import Path
stage=Path({path!r});root=Path('/opt/china2go-ai')
for name,digest in json.loads({json.dumps(info['manifest'])!r}).items():
 source=stage/name;assert hashlib.sha256(source.read_bytes()).hexdigest()==digest,name
 target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
print('reviewed_files_installed')
''', "install scoped release")
        remote.run("set -e\ncd /opt/china2go-ai/backend\n../venv/bin/python -m alembic upgrade head\n../venv/bin/python -m alembic check", label="migrate and verify production schema")
        assert guards(remote) == info["before"]
        started = True
        remote.run(f"systemctl start {SERVICES}", label="start deployed services")
        remote.run("for i in $(seq 1 25); do if curl -fsS http://127.0.0.1:8000/v1/health; then exit 0; fi; sleep 2; done; exit 1", label="API health", timeout=65)
        remote.run(f"systemctl is-active {SERVICES}", label="service health")
        after = guards(remote)
        assert after == info["before"], "protected_state_changed"
        remote_python(remote, f'''
import json,hashlib,sqlite3
from pathlib import Path
for name,digest in json.loads({json.dumps(info['manifest'])!r}).items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
with sqlite3.connect('file:backend/data/app.db?mode=ro',uri=True) as db:
 assert db.execute('pragma foreign_key_check').fetchall()==[]
 assert db.execute('select version_num from alembic_version').fetchone()[0]=='d2f6a8b901ce'
print('installed_hashes_schema_and_foreign_keys_verified')
''', "verify installed artifact")
        report = {**info, "after": after, "status": "deployed", "services": "all active", "migration": "d2f6a8b901ce"}
        (OUT / "deploy.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"deployed": info["release"], "before": info["before"], "after": after, "backup": backup}))
    except Exception:
        if stopped:
            remote.run(f"systemctl stop {SERVICES}", label="pause failed deployment")
        if backup_ready and installed:
            remote_python(remote, f'''
import json,sqlite3,tarfile
from pathlib import Path
root=Path('/opt/china2go-ai');backup=Path({backup!r})
for name in json.loads((backup/'new-files.json').read_text()):
 target=root/name;assert target.resolve().is_relative_to(root) and not target.is_symlink()
 target.unlink(missing_ok=True)
with tarfile.open(backup/'code.tar.gz') as archive:archive.extractall(root)
if not {started!r}:
 with sqlite3.connect(backup/'app.db') as src,sqlite3.connect(root/'backend/data/app.db') as dst:src.backup(dst)
print('previous_code_restored_database_preserved_after_restart')
''', "restore previous release")
        if stopped:
            remote.run(f"systemctl start {SERVICES}", label="restore previous services")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["stage", "deploy"])
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    remote = Remote(read_env(ROOT / ".env.deploy"))
    try:
        (stage if args.action == "stage" else deploy)(remote)
    finally:
        remote.close()


if __name__ == "__main__":
    main()
