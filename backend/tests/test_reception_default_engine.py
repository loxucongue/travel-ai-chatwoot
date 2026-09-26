from app.config import settings
from app.reception_rollout import default_engine_assignment
from app.reception_v2 import ENGINE_RELEASE_ID


def test_default_engine_assignment_keeps_v1_available(monkeypatch):
    monkeypatch.setattr(settings, "ai_engine_default", "v1")
    assert default_engine_assignment() == {
        "ai_engine_version": "v1",
        "ai_engine_release_id": "v1",
    }


def test_default_engine_assignment_stamps_current_v2_release(monkeypatch):
    monkeypatch.setattr(settings, "ai_engine_default", "v2")
    assert default_engine_assignment() == {
        "ai_engine_version": "v2",
        "ai_engine_release_id": ENGINE_RELEASE_ID,
    }
