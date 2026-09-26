from sqlalchemy import select

from app.models import AuditLog, ConversationState, InboxBinding


def _seed_state(session_factory) -> int:
    with session_factory() as db:
        inbox = InboxBinding(
            tenant_id=1, chatwoot_inbox_id=128859, name="Test", channel_type="test"
        )
        db.add(inbox)
        db.flush()
        state = ConversationState(
            tenant_id=1, inbox_binding_id=inbox.id, chatwoot_conversation_id=10
        )
        db.add(state)
        db.commit()
        return state.id


def test_conversation_engine_switch_is_versioned_and_audited(authenticated, session_factory):
    client, csrf = authenticated
    state_id = _seed_state(session_factory)

    response = client.patch(
        f"/v1/automation/conversations/{state_id}/engine",
        headers={"X-CSRF-Token": csrf},
        json={"engine_version": "v2", "expected_version": 1},
    )

    assert response.status_code == 200
    assert response.json()["engine_version"] == "v2"
    assert response.json()["version"] == 2
    with session_factory() as db:
        state = db.get(ConversationState, state_id)
        assert state.ai_engine_version == "v2"
        assert state.ai_engine_release_id != "v1"
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "conversation.engine_changed"))
        assert audit is not None

    conflict = client.patch(
        f"/v1/automation/conversations/{state_id}/engine",
        headers={"X-CSRF-Token": csrf},
        json={"engine_version": "v1", "expected_version": 1},
    )
    assert conflict.status_code == 409
