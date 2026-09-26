"""Deploy the V2 reception preview while preserving V1 defaults and outbound state."""
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

OUT = ROOT / "output/reception-v2-preview/deployment"
SERVICES = "china2go-api china2go-worker china2go-playground"
MIGRATION = "backend/alembic/versions/e3a91c04f7b2_reception_engine_coexistence.py"
APP_FILES = [
    "backend/app/automation_api.py", "backend/app/automation_models.py",
    "backend/app/automation_service.py", "backend/app/decision_service.py",
    "backend/app/evaluation_api.py", "backend/app/live_reply.py",
    "backend/app/live_reply_models.py", "backend/app/live_sop.py",
    "backend/app/models.py", "backend/app/release_provenance.py",
]


def guards(remote):
    return json.loads(remote.run(BASELINE, label="protected production state").strip().splitlines()[-1])


def remote_python(remote, code, label):
    return remote.run("cd /opt/china2go-ai && venv/bin/python - <<'PY'\n" + code + "\nPY", label=label)


def release_files():
    names = [*APP_FILES, MIGRATION]
    names += [p.relative_to(ROOT).as_posix() for p in (ROOT / "backend/app/reception_v2").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    names += [p.relative_to(ROOT).as_posix() for p in (ROOT / "frontend/dist").rglob("*") if p.is_file()]
    return sorted(set(names))


def stage(remote):
    before = guards(remote)
    assert before["sending"] is False, "sending_must_remain_disabled"
    remote_python(remote, """
import sqlite3
with sqlite3.connect('file:backend/data/app.db?mode=ro', uri=True) as db:
 assert db.execute('select version_num from alembic_version').fetchone()[0]=='d2f6a8b901ce'
print('expected_production_revision')
""", "verify deployment baseline")
    names = release_files()
    manifest = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = "reception-v2-preview-" + stamp
    stage_path = "/opt/china2go-ai/releases/" + release
    archive = OUT / (release + ".tar.gz")
    OUT.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as bundle:
        for name in names:
            bundle.add(ROOT / name, arcname=name)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    upload = "/tmp/" + archive.name
    with remote.client.open_sftp() as sftp:
        sftp.put(str(archive), upload)
    remote_python(remote, f"""
import hashlib,json,tarfile,shutil,sqlite3
from pathlib import Path
root=Path('/opt/china2go-ai');stage=Path({stage_path!r})
assert hashlib.sha256(Path({upload!r}).read_bytes()).hexdigest()=={archive_hash!r}
stage.mkdir()
manifest=json.loads({json.dumps(manifest)!r})
with tarfile.open({upload!r}) as bundle:
 for member in bundle.getmembers(): assert member.isfile() and member.name in manifest
 bundle.extractall(stage)
for name,digest in manifest.items(): assert hashlib.sha256((stage/name).read_bytes()).hexdigest()==digest,name
baseline={{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in {APP_FILES!r} if (root/name).is_file()}}
check=stage/'migration-check/backend'
shutil.copytree(root/'backend/app',check/'app',ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(root/'backend/alembic',check/'alembic',ignore=shutil.ignore_patterns('__pycache__'))
for name in {APP_FILES!r}:
 target=check/Path(name).relative_to('backend');target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(stage/name,target)
shutil.copytree(stage/'backend/app/reception_v2',check/'app/reception_v2',dirs_exist_ok=True)
shutil.copy2(stage/{MIGRATION!r},check/'alembic/versions'/Path({MIGRATION!r}).name)
shutil.copy2(root/'backend/alembic.ini',check/'alembic.ini')
shutil.copy2(root/'backend/.env',check/'.env');(check/'.env').chmod(0o600)
(check/'data').mkdir()
with sqlite3.connect(root/'backend/data/app.db') as src,sqlite3.connect(check/'data/app.db') as dst:src.backup(dst)
(stage/'manifest.json').write_text(json.dumps(manifest,indent=2))
(stage/'baseline.json').write_text(json.dumps(baseline,indent=2))
print(json.dumps({{'release':{release!r},'files':len(manifest)}}))
""", "verify and isolate release")
    remote.run(
        f"cd {shlex.quote(stage_path)}/migration-check/backend\n"
        "export APP_PROFILE=evaluation OUTBOUND_MODE=disabled CHATWOOT_WRITE_ENABLED=false LIVE_SOP_ENABLED=false\n"
        f"export DATABASE_URL=sqlite:///{stage_path}/migration-check/backend/data/app.db\n"
        "set -e\n/opt/china2go-ai/venv/bin/python -m alembic upgrade head\n"
        "/opt/china2go-ai/venv/bin/python -m alembic check",
        label="rehearse production-snapshot migration",
    )
    info = {"release": release, "stage": stage_path, "archive": str(archive), "archive_sha256": archive_hash,
            "manifest": manifest, "before": before, "backup": "/opt/china2go-ai/backups/" + release}
    (OUT / "stage.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps({"staged": release, "migration_rehearsal": "passed"}))


def deploy(remote):
    info = json.loads((OUT / "stage.json").read_text(encoding="utf-8"))
    assert guards(remote) == info["before"], "production_settings_changed"
    for name, digest in info["manifest"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, "local_source_changed:" + name
    stage_path, backup = info["stage"], info["backup"]
    remote_python(remote, f"""
import hashlib,json
from pathlib import Path
for name,digest in json.loads((Path({stage_path!r})/'baseline.json').read_text()).items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
print('production_source_unchanged')
""", "check unchanged production source")
    stopped = backup_ready = installed = started = False
    try:
        remote.run(f"systemctl stop {SERVICES}", label="pause services for migration")
        stopped = True
        assert guards(remote) == info["before"]
        remote_python(remote, f"""
import json,shutil,sqlite3,tarfile
from pathlib import Path
root=Path('/opt/china2go-ai');backup=Path({backup!r});backup.mkdir()
manifest=json.loads({json.dumps(info['manifest'])!r});existing=[name for name in manifest if (root/name).is_file()]
with tarfile.open(backup/'code.tar.gz','w:gz') as archive:
 for name in existing: archive.add(root/name,arcname=name)
(backup/'new-files.json').write_text(json.dumps([name for name in manifest if name not in existing]))
shutil.copy2(root/'backend/.env',backup/'backend.env');(backup/'backend.env').chmod(0o600)
with sqlite3.connect(root/'backend/data/app.db') as src,sqlite3.connect(backup/'app.db') as dst:src.backup(dst)
with sqlite3.connect(backup/'app.db') as db: assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
print('database_and_code_backup_verified')
""", "backup production")
        backup_ready = True
        installed = True
        remote_python(remote, f"""
import json,hashlib,shutil
from pathlib import Path
stage=Path({stage_path!r});root=Path('/opt/china2go-ai')
for name,digest in json.loads({json.dumps(info['manifest'])!r}).items():
 source=stage/name;assert hashlib.sha256(source.read_bytes()).hexdigest()==digest,name
 target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
print('preview_release_installed')
""", "install preview release")
        remote.run("set -e\ncd /opt/china2go-ai/backend\n../venv/bin/python -m alembic upgrade head\n../venv/bin/python -m alembic check", label="migrate production")
        assert guards(remote) == info["before"]
        remote.run(f"systemctl start {SERVICES}", label="start services")
        started = True
        remote.run("for i in $(seq 1 25); do if curl -fsS http://127.0.0.1:8000/v1/health; then exit 0; fi; sleep 2; done; exit 1", label="API health", timeout=65)
        remote.run(f"systemctl is-active {SERVICES}", label="service health")
        after = guards(remote)
        assert after == info["before"], "protected_state_changed"
        remote_python(remote, """
import sqlite3
with sqlite3.connect('file:backend/data/app.db?mode=ro',uri=True) as db:
 assert db.execute('pragma foreign_key_check').fetchall()==[]
 assert db.execute('select version_num from alembic_version').fetchone()[0]=='e3a91c04f7b2'
 assert db.execute("select count(*) from conversation_states where ai_engine_version!='v1'").fetchone()[0]==0
print('schema_valid_all_existing_conversations_v1')
""", "verify disabled preview")
        report = {**info, "after": after, "status": "deployed", "migration": "e3a91c04f7b2", "v2_live_enabled": False}
        (OUT / "deploy.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"deployed": info["release"], "before": info["before"], "after": after, "backup": backup}))
    except Exception:
        if stopped: remote.run(f"systemctl stop {SERVICES}", label="pause failed deployment")
        if backup_ready and installed:
            remote_python(remote, f"""
import json,sqlite3,tarfile
from pathlib import Path
root=Path('/opt/china2go-ai');backup=Path({backup!r})
for name in json.loads((backup/'new-files.json').read_text()):
 target=root/name;assert target.resolve().is_relative_to(root) and not target.is_symlink();target.unlink(missing_ok=True)
with tarfile.open(backup/'code.tar.gz') as archive: archive.extractall(root)
if not {started!r}:
 with sqlite3.connect(backup/'app.db') as src,sqlite3.connect(root/'backend/data/app.db') as dst:src.backup(dst)
print('previous_release_restored')
""", "restore previous release")
        if stopped: remote.run(f"systemctl start {SERVICES}", label="restore services")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["stage", "deploy"])
    args = parser.parse_args()
    remote = Remote(read_env(ROOT / ".env.deploy"))
    try:
        (stage if args.action == "stage" else deploy)(remote)
    finally:
        remote.close()


if __name__ == "__main__":
    main()
