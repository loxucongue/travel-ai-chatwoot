import pytest
from sqlalchemy import select

from app.automation_models import AutomationSession, RehearsalJob, TouchReservation
from app.automation_service import advance_sops, enroll_rehearsal, sop_snapshot, add_customer_message
from app.models import SopDefinition, StoredMedia, OutboundMessage
from app.sop_schedule import schedule_at, schedule_preview, content_items

ADDED = "2026-08-26T02:00:00+00:00"


def text(key, value):
    return {"key": key, "content_type": "text", "content": value}


def node(key="a", **kwargs):
    return {"key": key, "schedule_type": "relative", "basis": "customer_added", "delay_minutes": 10,
            "messages": [text("one", "hello")], **kwargs}


def setup(db, nodes):
    session = AutomationSession(owner_id=1, mode="sop", virtual_now=ADDED,
        controls={"customer_added_at": ADDED, "can_reply": True, "channel": "facebook", "ai_enabled": True},
        messages=[{"direction": "incoming", "content": "hello", "created_at": ADDED}])
    sop = SopDefinition(tenant_id=1, name="groups", status="running", created_by=1, nodes=nodes)
    db.add_all([session, sop]); db.flush()
    version = sop_snapshot(db, sop, 1)
    enrollment = enroll_rehearsal(db, session, version)
    db.flush()
    return session, version, enrollment


@pytest.mark.parametrize(("day", "expected"), [(1, "2026-08-26T01:30:00+00:00"), (2, "2026-08-27T01:30:00+00:00"), (3, "2026-08-28T01:30:00+00:00")])
def test_day_number_is_shanghai_calendar_day(day, expected):
    result = schedule_at(node(schedule_type="calendar_day", day_number=day, time_of_day="09:30"),
                         customer_added_at="2026-08-26T15:55:00+00:00", enrolled_at=ADDED)
    assert result == expected


def test_added_time_is_not_enrollment_and_previous_requires_confirmation():
    assert schedule_at(node(), customer_added_at=ADDED, enrolled_at="2026-08-26T04:00:00+00:00") == "2026-08-26T02:10:00+00:00"
    assert schedule_at(node(basis="previous_node"), customer_added_at=ADDED, enrolled_at=ADDED) is None
    assert schedule_at(node(basis="previous_node"), customer_added_at=ADDED, enrolled_at=ADDED,
                       previous_at="2026-08-26T02:13:00+00:00") == "2026-08-26T02:23:00+00:00"
    with pytest.raises(ValueError, match="customer_added_time_missing"):
        schedule_at(node(), customer_added_at=None, enrolled_at=ADDED)


def test_relative_schedule_supports_sub_minute_initial_delivery_gaps():
    result = schedule_at(
        node(basis="previous_node", delay_minutes=0, delay_seconds=3),
        customer_added_at=ADDED,
        enrolled_at=ADDED,
        previous_at="2026-08-26T02:00:06+00:00",
    )
    assert result == "2026-08-26T02:00:09+00:00"


def test_preview_does_not_hide_window_frequency_or_past_time_conflicts():
    rows = schedule_preview([node(), node("b", delay_minutes=20), node("c", schedule_type="calendar_day", day_number=3, time_of_day="10:00")], ADDED, 24)
    assert rows[0]["scheduled_at"] == "2026-08-26T02:10:00+00:00"
    assert "contact_frequency_limit" not in rows[1]["warnings"]
    assert "outside_initial_channel_window" in rows[2]["warnings"]
    past = schedule_preview([node(schedule_type="calendar_day", day_number=1, time_of_day="09:00")], ADDED, 24)
    assert "before_customer_added" in past[0]["warnings"]


def test_group_is_ordered_atomic_and_counts_one_touch(session_factory, tmp_path):
    with session_factory() as db:
        path = tmp_path / "image.png"; path.write_bytes(b"test image")
        media = StoredMedia(tenant_id=1, original_name="test.png", media_type="image", mime_type="image/png", file_size=10, storage_path=str(path), created_by=1)
        db.add(media); db.flush()
        first = node(messages=[text("text", "first"), {"key": "image", "content_type": "image", "media_id": media.id}, text("last", "third")])
        session, _, _ = setup(db, [first, node("b", basis="previous_node")])
        session.virtual_now = "2026-08-26T02:10:00+00:00"
        advance_sops(db, session); db.flush()
        sent = session.messages[1:]
        assert [x["content_type"] for x in sent] == ["text", "image", "text"]
        assert [x["item_key"] for x in sent] == ["text", "image", "last"]
        assert len(db.scalars(select(TouchReservation)).all()) == 1
        jobs = db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        assert len(jobs[0].payload["delivery_items"]) == 3
        assert jobs[1].scheduled_at == "2026-08-26T02:20:00+00:00"
        advance_sops(db, session)
        assert len(session.messages) == 4
        session.virtual_now = "2026-08-26T02:20:00+00:00"
        advance_sops(db, session)
        assert jobs[1].status == "simulated_delivered"
        assert len(session.messages) == 5
        assert db.scalar(select(OutboundMessage)) is None


def test_missing_media_rejected_before_publication(session_factory):
    with session_factory() as db, pytest.raises(ValueError, match="material_unavailable"):
        setup(db, [node(messages=[{"key": "image", "content_type": "image", "media_id": 999}])])


@pytest.mark.parametrize("mutation,reason", [
    ("missing", "material_unavailable"),
    ("changed", "material_revision_changed"),
])
def test_missing_media_blocks_entire_group_and_dependency(session_factory, tmp_path, mutation, reason):
    with session_factory() as db:
        path = tmp_path / "removed.png"
        path.write_bytes(b"image")
        media = StoredMedia(tenant_id=1, original_name="removed.png", media_type="image", mime_type="image/png", file_size=5, storage_path=str(path), created_by=1)
        db.add(media); db.flush()
        session, _, _ = setup(db, [node(messages=[text("text", "must not send"), {"key": "image", "content_type": "image", "media_id": media.id}]), node("b", basis="previous_node")])
        if mutation == "missing":
            path.unlink()
        else:
            path.write_bytes(b"changed image")
        session.virtual_now = "2026-08-26T02:10:00+00:00"
        advance_sops(db, session); db.flush()
        jobs = db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        assert jobs[0].reason == reason
        assert jobs[0].confirmed_at is None
        assert jobs[1].reason == "predecessor_not_confirmed"
        assert jobs[1].confirmed_at is None
        assert len(session.messages) == 1
        assert not db.scalars(select(TouchReservation)).all()
        assert db.scalar(select(OutboundMessage)) is None


def test_day_three_survives_enrollment_ttl_but_window_still_blocks(session_factory):
    with session_factory() as db:
        session, version, enrollment = setup(db, [node(schedule_type="calendar_day", day_number=3, time_of_day="10:00")])
        assert version.config["ttl_hours"] > 48
        session.virtual_now = "2026-08-28T02:00:00+00:00"
        assert enrollment.expires_at > session.virtual_now
        advance_sops(db, session)
        job = db.scalar(select(RehearsalJob))
        assert job.reason == "automatic_window_closed"


def test_added_anchor_frozen_and_new_reply_cancels_group(session_factory):
    with session_factory() as db:
        session, _, _ = setup(db, [node()])
        session.virtual_now = "2026-08-26T02:05:00+00:00"
        add_customer_message(db, session, "another", "new")
        assert session.controls["customer_added_at"] == ADDED
        assert db.scalar(select(RehearsalJob)).status == "cancelled"




def test_old_single_message_normalizes_without_loss():
    assert content_items({"content_type": "image", "media_id": 5, "content": "caption"}) == [{"key": "legacy", "content_type": "image", "media_id": 5, "content": "caption"}]


def test_empty_upload_rejected(authenticated):
    client, csrf = authenticated
    result = client.post('/v1/media', files={"file": ("empty.png", b"", "image/png")}, headers={"X-CSRF-Token": csrf})
    assert result.status_code == 422


def test_local_video_preview_supports_inline_range(authenticated, session_factory, tmp_path):
    client, _ = authenticated
    path = tmp_path / "clip.webm"
    path.write_bytes(b"video header test bytes")
    with session_factory() as db:
        media = StoredMedia(tenant_id=1, original_name="clip.webm", media_type="video", mime_type="video/webm", file_size=23, storage_path=str(path), created_by=1)
        db.add(media); db.commit(); media_id = media.id
    response = client.get(f'/v1/media/{media_id}/preview', headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.headers['content-type'] == 'video/webm'
    assert response.headers['content-disposition'] == 'inline'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.content == b"video"


def test_real_history_added_anchor_is_scoped_to_contact_and_inbox(authenticated, session_factory):
    from app.models import Contact, InboxBinding, ConversationState, MessageEvent
    client, csrf = authenticated
    with session_factory() as db:
        db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=7, name="customer"))
        db.add_all([InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=101, name="one"), InboxBinding(id=2, tenant_id=1, chatwoot_inbox_id=102, name="two")])
        db.flush()
        db.add_all([ConversationState(id=1, tenant_id=1, contact_id=1, inbox_binding_id=1, chatwoot_conversation_id=1), ConversationState(id=2, tenant_id=1, contact_id=1, inbox_binding_id=2, chatwoot_conversation_id=2)])
        db.flush()
        db.add_all([MessageEvent(conversation_state_id=1, chatwoot_message_id=1, direction="incoming", content="first", created_at=ADDED),
                    MessageEvent(conversation_state_id=2, chatwoot_message_id=2, direction="incoming", content="other inbox", created_at="2026-01-01T00:00:00+00:00"),
                    MessageEvent(conversation_state_id=1, chatwoot_message_id=3, direction="outgoing", content="not a customer", created_at="2026-01-01T00:00:00+00:00")])
        db.commit()
    response = client.post('/v1/playground/sessions', json={"mode": "sop", "conversation_id": 1, "virtual_now": "2026-08-26T04:00:00+00:00"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201
    assert response.json()['controls']['customer_added_at'] == ADDED
