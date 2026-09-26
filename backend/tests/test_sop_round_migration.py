from alembic import command
from alembic.config import Config
from pathlib import Path
from sqlalchemy import create_engine, text, Table, MetaData
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Tenant, User, SopDefinition


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_migration_preserves_jobs_and_closes_duplicate_active_rounds(tmp_path, monkeypatch):
    url=f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setattr(settings,'database_url',url)
    config=Config(str(BACKEND_ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND_ROOT / 'alembic'))
    command.upgrade(config,'3352b5e45e19')
    engine=create_engine(url)
    with Session(engine) as db:
        db.add(Tenant(id=1,name='test'));db.add(User(id=1,email='test@example.com',display_name='test',password_hash='unused'))
        db.flush()
        # Seed the historical schema, not the current ORM's newer columns.
        historical = MetaData()
        old_sops = Table('sop_definitions', historical, autoload_with=engine)
        old_sessions = Table('automation_sessions', historical, autoload_with=engine)
        old_versions = Table('sop_versions', historical, autoload_with=engine)
        db.execute(old_sops.insert().values(id=1,tenant_id=1,created_by=1,name='legacy',description='',status='draft',version=1,
            dry_run=True,live_enabled=False,trigger_type='manual',trigger_labels=[],inbox_ids=[],nodes=[],exit_labels=[],
            stop_on_incoming=True,frequency_hours=24,created_at='2026-08-26T02:00:00+00:00',updated_at='2026-08-26T02:00:00+00:00'))
        db.execute(old_sessions.insert().values(
            id=1, owner_id=1, mode='sop', environment='playground', generation=0,
            virtual_now='2026-08-26T02:00:00+00:00', messages=[], memory={}, controls={},
            created_at='2026-08-26T02:00:00+00:00',
        ))
        db.flush()
        db.execute(old_versions.insert().values(
            id=1, sop_id=1, version=1, created_by=1, content_hash='test', config={},
            created_at='2026-08-26T02:00:00+00:00',
        ))
        db.flush()
        for i in (1,2):
            db.execute(text("INSERT INTO rehearsal_enrollments (id,session_id,sop_version_id,generation,status,enrolled_at,expires_at) VALUES (:id,1,1,:id,'active','2026-08-26T02:00:00+00:00','2026-08-27T02:00:00+00:00')"),{'id':i})
            db.execute(text("INSERT INTO rehearsal_jobs (id,enrollment_id,node_key,status,payload,created_at) VALUES (:id,:id,'node','scheduled','{}','2026-08-26T02:00:00+00:00')"),{'id':i})
        db.commit()
    command.upgrade(config,'head')
    with engine.connect() as db:
        assert db.execute(text('SELECT round_number,status FROM rehearsal_enrollments ORDER BY id')).all()==[(1,'cancelled'),(2,'active')]
        assert db.execute(text('SELECT status FROM rehearsal_jobs ORDER BY id')).all()==[('cancelled',),('scheduled',)]
        assert db.execute(text('PRAGMA foreign_key_check')).all()==[]
    engine.dispose()
