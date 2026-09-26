import base64
from copy import deepcopy
import hashlib

from app.automation_models import AutomationRun, AutomationSession, RehearsalEnrollment, RehearsalJob
from app.automation_service import advance_sops, confirm_draft, sop_snapshot, subject_key
from app.material_library import CATALOG_VERSION
from app.models import InboxBinding, KnowledgeVersion, MaterialAsset, SopDefinition, StoredMedia
from app.route_packages import ROUTES
from app.route_reply import ROUTE_SNAPSHOTS_KEY, make_route_snapshot


ROUTE = "peach_9d_2027"
AT = "2026-09-13T02:00:00+00:00"


def test_static_sop_real_delivery_metadata_visible_in_session_api(
    authenticated, session_factory, tmp_path, monkeypatch,
):
    client, _ = authenticated
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8WQAAAAASUVORK5CYII=")
    path = tmp_path / "reviewed.png"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with session_factory() as db:
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Test", channel_type="facebook")
        knowledge = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION, title="Test", content_hash=digest)
        media = StoredMedia(tenant_id=1, original_name=path.name, media_type="image", mime_type="image/png",
                            file_size=len(data), storage_path=str(path), created_by=1)
        db.add_all([inbox, knowledge, media])
        db.flush()
        db.add(MaterialAsset(
            knowledge_version_id=knowledge.id, asset_key="frozen-image", source_path=str(path),
            display_name="Frozen image", available=True, media_type="image", file_hash=digest,
            metadata_json={"stored_media_id": media.id, "route_variants": [ROUTE],
                           "content_family": "frozen-image", "review_state": "evaluation_ready"},
        ))
        spec = deepcopy(ROUTES[ROUTE])
        spec["package_version"] = "historical-package"
        spec["groups"]["hotel_reference"].update(text="Frozen text.", assets=["frozen-image"])
        snapshot = make_route_snapshot(ROUTE, spec)
        sop = SopDefinition(
            tenant_id=1, name="Static frozen SOP", status="running", version=1, route_variant=ROUTE,
            inbox_ids=[128859], created_by=1,
            nodes=[{"key": "hotel", "content_group_key": "hotel_reference", "delivery_interval_seconds": 2,
                    "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
                    "messages": [
                        {"key": "photo", "content_type": "image", "content": "", "media_id": media.id,
                         "asset_key": "frozen-image"},
                        {"key": "text", "content_type": "text", "content": "Frozen text."},
                    ]}],
        )
        db.add(sop)
        db.flush()
        version = sop_snapshot(db, sop, 1)
        session = AutomationSession(
            owner_id=1, inbox_binding_id=inbox.id, mode="journey", environment="playground", virtual_now=AT,
            controls={"can_reply": True, "route_variant": ROUTE,
                      "journey": {"route_variant": ROUTE, "slots": {ROUTE_SNAPSHOTS_KEY: {ROUTE: snapshot}},
                                  "sent_content_groups": []}},
            messages=[{"id": "customer", "direction": "incoming", "content": "Question", "created_at": AT}],
        )
        db.add(session)
        db.flush()
        enrollment = RehearsalEnrollment(
            session_id=session.id, sop_id=sop.id, sop_version_id=version.id, subject_key=subject_key(db, session),
            generation=session.generation, enrolled_at=AT, expires_at="2026-09-14T02:00:00+00:00",
        )
        db.add(enrollment)
        db.flush()
        job = RehearsalJob(enrollment_id=enrollment.id, node_key="hotel", scheduled_at=AT,
                           payload=deepcopy(version.config["nodes"][0]))
        db.add(job)
        db.commit()
        session_id, version_id = session.id, version.id
        # Runtime metadata must not consult the now-unpublished route catalog.
        monkeypatch.delitem(ROUTES, ROUTE)
        advance_sops(db, session)
        db.commit()
        assert job.status == "simulated_delivered"

    response = client.get(f"/v1/playground/sessions/{session_id}")
    assert response.status_code == 200
    detail = response.json()
    messages = [item for item in detail["messages"] if item.get("source") == "sop"]
    assert len(messages) == 2
    assert messages[0]["asset_key"] == "frozen-image"
    assert messages[0]["media_hash"] == digest
    assert [item["content"] for item in messages] == ["", "Frozen text."]
    recorded = detail["jobs"][0]["payload"]["delivery_items"]
    for message, item in zip(messages, recorded):
        metadata = message["content_attributes"]["delivery_item"]
        assert message["content_group_key"] == metadata["group_key"] == "hotel_reference"
        assert metadata["item_id"] == message["id"] == item["item_id"]
        assert metadata["status"] == message["status"] == item["status"] == "simulated_delivered"
        assert metadata["confirmed_at"] == message["created_at"] == item["confirmed_at"]
        assert metadata["sop_version_id"] == version_id
        assert metadata["package_version"] == "historical-package"
        assert metadata["snapshot_digest"] == snapshot["digest"]
        assert metadata["plan_version"]


def test_draft_confirmation_updates_public_item_status_without_mocks(authenticated, session_factory):
    client, _ = authenticated
    with session_factory() as db:
        session = AutomationSession(
            owner_id=1, mode="journey", environment="playground", virtual_now=AT, controls={"can_reply": True},
        )
        db.add(session)
        db.flush()
        run = AutomationRun(session_id=session.id, module="reply", generation=session.generation,
                            idempotency_key="public-status", decision={"action": "reply"})
        db.add(run)
        db.flush()
        session.messages = [
            {"id": "customer", "direction": "incoming", "content": "Question", "created_at": AT},
            {"id": "draft", "run_id": run.id, "direction": "outgoing", "content": "Answer.",
             "created_at": AT, "status": "draft", "content_attributes": {
                 "delivery_item": {"item_id": "stable-id", "plan_version": "frozen-plan", "status": "draft"}}},
        ]
        db.commit()
        confirm_draft(db, session, "draft")
        db.commit()
        session_id = session.id
    response = client.get(f"/v1/playground/sessions/{session_id}")
    assert response.status_code == 200
    message = next(item for item in response.json()["messages"] if item.get("id") == "draft")
    assert message["content_attributes"]["delivery_item"] == {
        "item_id": "stable-id", "plan_version": "frozen-plan", "status": "simulated_delivered", "confirmed_at": AT,
    }
