from app.models import AppSetting
from app.reception_rollout import AI_RECEPTION_ROLLOUT_KEY, reception_rollout


def test_reception_rollout_uses_environment_default_when_not_persisted(session_factory, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26, 42")
    with session_factory() as db:
        rollout = reception_rollout(db)
    assert rollout.allowlist_enabled is True
    assert rollout.conversation_ids == frozenset({26, 42})
    assert rollout.allows(26) is True
    assert rollout.allows(99) is False


def test_persisted_rollout_can_disable_conversation_allowlist(session_factory, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26")
    with session_factory() as db:
        db.add(AppSetting(
            key=AI_RECEPTION_ROLLOUT_KEY,
            value={"allowlist_enabled": False, "conversation_ids": [26]},
        ))
        db.commit()
        rollout = reception_rollout(db)
    assert rollout.scope == "ai_label"
    assert rollout.allows(26) is True
    assert rollout.allows(9999) is True


def test_invalid_persisted_rollout_fails_back_to_narrow_environment_scope(session_factory, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26")
    with session_factory() as db:
        db.add(AppSetting(
            key=AI_RECEPTION_ROLLOUT_KEY,
            value={"allowlist_enabled": "no", "conversation_ids": ["bad"]},
        ))
        db.commit()
        rollout = reception_rollout(db)
    assert rollout.scope == "allowlist"
    assert rollout.conversation_ids == frozenset({26})

