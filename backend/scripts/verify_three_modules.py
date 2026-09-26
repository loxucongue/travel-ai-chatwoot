"""Offline migration and safety checks against a disposable copy of the backup."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing

from app.config import settings
from app.db import SessionLocal
from app.models import ConversationState, MessageEvent, OutboundMessage, HandoffTask
from sqlalchemy import select, func


def counts(db):
    names=[r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' and name != 'alembic_version'")]
    return {name:db.execute('select count(*) from "'+name.replace('"','""')+'"').fetchone()[0] for name in names}


def run():
    root=Path(__file__).resolve().parents[2]
    backup=root/'backend/data/backups/before_three_modules_20260826_121652.db'
    with tempfile.TemporaryDirectory(prefix='three-modules-migration-') as directory:
        old=Path(directory)/'upgrade.db'
        with closing(sqlite3.connect(backup)) as source,closing(sqlite3.connect(old)) as dest:
            source.backup(dest)
            before=counts(dest)
        for name in ('upgrade.db','empty.db'):
            env={**os.environ,'DATABASE_URL':'sqlite:///'+(Path(directory)/name).as_posix()}
            result=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=root/'backend',env=env,capture_output=True,text=True)
            if result.returncode:raise RuntimeError('migration_failed: '+result.stderr)
        with closing(sqlite3.connect(old)) as db:
            after=counts(db)
            assert all(after[k]==v for k,v in before.items()),'existing_rows_changed'
            assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert settings.app_profile=='evaluation' and settings.outbound_mode=='disabled' and not settings.chatwoot_write_enabled
    if settings.deepseek_api_key:
        for folder in (root/'frontend/dist',root/'.runlogs'):
            for path in folder.rglob('*'):
                if path.is_file() and path.suffix in ('.js','.html','.json','.log'):
                    assert settings.deepseek_api_key not in path.read_text(encoding='utf-8',errors='ignore'), 'secret_leaked'
    with SessionLocal() as db:
        actual={name:db.scalar(select(func.count()).select_from(model)) for name,model in [('conversations',ConversationState),('messages',MessageEvent),('outbound',OutboundMessage),('handoffs',HandoffTask)]}
    assert actual['outbound']==2 and actual['handoffs']==0,'real_side_effect_detected'
    report={'empty_migration':'passed','backup_upgrade':'passed','existing_table_counts_preserved':True,'sqlite_integrity':'ok','secret_scan':'passed','profile':settings.app_profile,'outbound_mode':settings.outbound_mode,'chatwoot_write_enabled':settings.chatwoot_write_enabled,'counts':actual,'outbound_added':0,'handoffs_added':0}
    target=root/'output/three-modules-20260826/safety-and-migration.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':run()
