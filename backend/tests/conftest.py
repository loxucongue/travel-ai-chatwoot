import os

# Set these before app.config is imported: a fixture runs too late to prevent
# startup validation (or initialization) from inheriting a developer's live .env.
os.environ.update(APP_PROFILE="evaluation", OUTBOUND_MODE="disabled",
                  CHATWOOT_WRITE_ENABLED="false", LIVE_SOP_ENABLED="false",
                  DATABASE_URL="sqlite://", RELAY_BASE_URL="", RELAY_API_TOKEN="")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Tenant, User
from app.security import hash_password


@pytest.fixture(autouse=True)
def safe_test_runtime(monkeypatch):
    # Tests must not inherit a developer's live .env or allow customer sends.
    from app.config import settings
    monkeypatch.setattr(settings, "app_profile", "evaluation")
    monkeypatch.setattr(settings, "outbound_mode", "disabled")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", False)
    monkeypatch.setattr(settings, "live_sop_enabled", False)


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=1, name="test"))
        db.add(User(email="admin@example.com", display_name="Admin", password_hash=hash_password("password123"), role="super_admin"))
        db.commit()
    yield factory
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(session_factory):
    def override_db():
        with session_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as value:
        yield value
    app.dependency_overrides.clear()


@pytest.fixture()
def authenticated(client):
    response = client.post("/v1/auth/login", json={"email": "admin@example.com", "password": "password123"})
    assert response.status_code == 200
    return client, response.json()["csrf_token"]
