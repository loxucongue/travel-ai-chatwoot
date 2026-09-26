from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.config import settings


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "d1e7a9c4b205"


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    engine = create_engine(url)
    yield config, engine
    engine.dispose()


def insert_user(connection, user_id, agent_id):
    connection.execute(text(
        "INSERT INTO users (id,email,display_name,password_hash,role,active,created_at,chatwoot_agent_id) "
        "VALUES (:id,:email,'test','unused','agent',1,'2026-09-16',:agent)"
    ), {"id": user_id, "email": f"{user_id}@example.com", "agent": agent_id})


def test_upgrade_preserves_personal_reads_and_matches_models(migration_db):
    config, engine = migration_db
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id,name,timezone,ai_enabled,created_at) VALUES (1,'test','UTC',1,'2026-09-16')"))
        insert_user(connection, 1, 777)
        for notification_id, user_id in ((1, 1), (2, None)):
            connection.execute(text(
                "INSERT INTO notifications (id,tenant_id,user_id,event_type,title,body,read_at,created_at) "
                "VALUES (:id,1,:user,'test','test','','2026-09-16','2026-09-16')"
            ), {"id": notification_id, "user": user_id})
    command.upgrade(config, "head")
    command.check(config)
    expected_engine_columns = {
        "conversation_states": {"ai_engine_version", "ai_engine_release_id"},
        "automation_sessions": {"engine_version", "engine_release_id"},
        "live_reply_jobs": {"engine_version", "engine_release_id"},
        "live_sop_enrollments": {"engine_version", "engine_release_id"},
    }
    for table_name, expected in expected_engine_columns.items():
        columns = {column["name"]: column for column in inspect(engine).get_columns(table_name)}
        assert expected <= columns.keys()
        assert all(columns[name]["nullable"] is False for name in expected)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT notification_id,user_id FROM notification_reads")).all() == [(1, 1)]
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                insert_user(connection, 2, 777)
        insert_user(connection, 3, None)
        insert_user(connection, 4, None)
    command.downgrade(config, PREVIOUS)
    assert "notification_reads" not in inspect(engine).get_table_names()
    assert "ai_engine_version" not in {column["name"] for column in inspect(engine).get_columns("conversation_states")}
    command.upgrade(config, "head")
    command.check(config)


def test_duplicate_bindings_abort_before_schema_changes(migration_db):
    config, engine = migration_db
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        insert_user(connection, 1, 777)
        insert_user(connection, 2, 777)
    with pytest.raises(RuntimeError, match="duplicate_chatwoot_agent_bindings"):
        command.upgrade(config, "head")
    assert "notification_reads" not in inspect(engine).get_table_names()
    assert "assignment_target_user_id" not in {column["name"] for column in inspect(engine).get_columns("handoff_tasks")}
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM users WHERE chatwoot_agent_id=777")).scalar() == 2
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == PREVIOUS
        connection.execute(text("UPDATE users SET chatwoot_agent_id=NULL WHERE id=2"))
    command.upgrade(config, "head")
    command.check(config)
