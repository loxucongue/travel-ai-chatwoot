from copy import deepcopy

import pytest

from app import automation_service as service
from app.automation_models import AutomationRun, AutomationSession
from app.route_packages import ROUTES
from app.route_reply import CONTENT_PROGRESS_KEY, ROUTE_SNAPSHOTS_KEY, make_route_snapshot


ROUTE = "peach_9d_2027"
AT = "2026-09-13T02:00:00+00:00"


def draft_session(db):
    spec = deepcopy(ROUTES[ROUTE])
    spec["sequence"] = ["hotel_reference"]
    spec["groups"]["hotel_reference"].update(
        assets=["old-asset"], text="Old reviewed hotel text.", initial_delivery=True,
        delivery_mode="assets_then_text",
    )
    spec["policies"]["contact_request_group"] = "entry_question"
    slots = {ROUTE_SNAPSHOTS_KEY: {ROUTE: make_route_snapshot(ROUTE, spec)}}
    session = AutomationSession(
        owner_id=1, mode="journey", environment="playground", virtual_now=AT,
        controls={"can_reply": True, "ai_enabled": True, "route_variant": ROUTE,
                  "journey": {"route_variant": ROUTE, "slots": slots, "sent_content_groups": []}},
    )
    db.add(session)
    db.flush()
    run = AutomationRun(
        session_id=session.id, module="reply", generation=session.generation,
        idempotency_key="snapshot-draft", status="completed",
        decision={"action": "reply", "route_variant": ROUTE,
                  "content_group_key": "hotel_reference", "covered_content_groups": ["entry_question"],
                  "lead_action": "ask", "follow_up_type": "contact", "follow_up_question": "Question?"},
    )
    db.add(run)
    db.flush()
    session.messages = [
        {"id": "customer", "direction": "incoming", "content": "Question", "created_at": AT},
        {"id": "text", "run_id": run.id, "direction": "outgoing", "status": "draft",
         "content": spec["groups"]["entry_question"]["text"], "created_at": AT},
        *[{"id": key, "run_id": run.id, "direction": "outgoing", "status": "draft",
           "asset_key": key, "media_id": index, "content_type": "image", "created_at": AT}
          for index, key in enumerate(["old-asset", "new-asset"], 1)],
    ]
    db.commit()
    return session, spec


@pytest.fixture
def isolated_delivery(monkeypatch):
    monkeypatch.setattr(service, "material_info", lambda _db, item, *_: item)
    monkeypatch.setattr(service, "previous_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "record_delivery", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "enroll_detected_journey_route", lambda *args: None)


@pytest.mark.parametrize("change", ["sequence", "follow_up_policy", "route_removed"])
def test_confirm_draft_uses_old_requirements_and_deferred_question(
    session_factory, monkeypatch, isolated_delivery, change,
):
    with session_factory() as db:
        session, original = draft_session(db)
        latest = deepcopy(ROUTES[ROUTE])
        latest["groups"]["hotel_reference"]["assets"] = ["new-asset"]
        if change == "sequence":
            latest["sequence"] = []
        elif change == "follow_up_policy":
            latest["policies"]["contact_request_group"] = "price_reference"
        if change == "route_removed":
            monkeypatch.delitem(ROUTES, ROUTE)
        else:
            monkeypatch.setitem(ROUTES, ROUTE, latest)

        service.confirm_draft(db, session, "text")
        journey = session.controls["journey"]
        progress = journey["slots"][CONTENT_PROGRESS_KEY][ROUTE]
        assert progress["hotel_reference"]["asset_keys"] == ["old-asset"]
        assert "entry_question" not in progress
        assert journey["sent_content_groups"] == []
        assert session.controls.get("lead_capture", {}).get("status", "not_started") == "not_started"
        assert journey["slots"][ROUTE_SNAPSHOTS_KEY][ROUTE]["spec"] == original


def test_confirm_draft_missing_snapshot_blocks_before_receipts(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = draft_session(db)
        session.controls = {**session.controls, "journey": {"slots": {}, "sent_content_groups": []}}
        original_messages = deepcopy(session.messages)
        monkeypatch.setattr(service, "material_info", lambda *args: pytest.fail("must block before material lookup"))
        with pytest.raises(ValueError, match="route_snapshot_unverifiable"):
            service.confirm_draft(db, session, "text")
        assert session.messages == original_messages


def test_confirm_draft_still_honors_current_global_gate(session_factory):
    with session_factory() as db:
        session, _ = draft_session(db)
        session.controls = {**session.controls, "ai_enabled": False}
        with pytest.raises(ValueError, match="ai_disabled"):
            service.confirm_draft(db, session, "text")
