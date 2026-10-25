"""Regressions for production management API errors found on October 19."""
from copy import deepcopy

import pytest

import app.automation_api as api
from app.automation_models import AutomationRun, AutomationSession
from app.models import AiRun, ConversationState, HandoffTask, InboxBinding, MessageEvent, User, UserInboxScope, utcnow
from app.route_packages import ROUTE_PACKAGES
from app.security import hash_password


@pytest.mark.parametrize("endpoint", ["overview", "funnel"])
def test_bi_handles_missing_handoff_deadlines(authenticated, session_factory, endpoint):
    client, _ = authenticated
    with session_factory() as db:
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=10, name="test"))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=100))
        db.flush()
        for status, due_at in [
            ("pending", None), ("claimed", None), ("pending", ""),
            ("pending", "2000-01-01T00:00:00+00:00"),
            ("claimed", "2000-01-01T00:00:00+00:00"),
            ("pending", "2099-01-01T00:00:00+00:00"),
            ("completed", "2000-01-01T00:00:00+00:00"),
        ]:
            db.add(HandoffTask(conversation_state_id=1, status=status, sla_due_at=due_at))
        db.commit()
    response = client.get(f"/v1/bi/{endpoint}")
    assert response.status_code == 200, response.text
    if endpoint == "overview":
        result = response.json()
        assert result["handoffs"] == 7
        assert result["handoff_pending"] == 4
        assert result["handoff_overdue"] == 2
    else:
        assert response.json()["items"][0]["value"] == 1


@pytest.mark.parametrize("purpose", [None, "", "Existing purpose"])
def test_route_catalog_reads_legacy_group_without_mutation(authenticated, monkeypatch, purpose):
    client, _ = authenticated
    route = "peach_9d_2027"
    package = deepcopy(ROUTE_PACKAGES[route])
    key = package["content_sequence"][0]
    if purpose is None:
        package["content_groups"][key].pop("purpose", None)
    else:
        package["content_groups"][key]["purpose"] = purpose
    before = deepcopy(package)
    monkeypatch.setitem(ROUTE_PACKAGES, route, package)
    monkeypatch.setattr(api, "ensure_route_packages_current", lambda: None)
    response = client.get("/v1/automation/route-products")
    assert response.status_code == 200, response.text
    product = next(item for item in response.json()["items"] if item["route_variant"] == route)
    group = next(item for item in product["content_groups"] if item["key"] == key)
    assert group["purpose"] == (purpose or key)
    assert group["approved_text"] == before["content_groups"][key]["approved_text"]
    assert package == before


def test_bi_includes_live_v3_activity_and_errors_only(authenticated, session_factory):
    client, _ = authenticated
    now = utcnow()
    old = "2000-01-01T00:00:00+00:00"
    with session_factory() as db:
        db.add_all([InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=10, name="live"),
                    InboxBinding(id=2, tenant_id=1, chatwoot_inbox_id=20, name="other")])
        db.flush()
        for cid in range(1, 7):
            db.add(ConversationState(id=cid, tenant_id=1, inbox_binding_id=2 if cid == 6 else 1,
                                     chatwoot_conversation_id=100 + cid))
        db.flush()
        # V3 messages are attributed to AI without a legacy AiRun record.
        cases = [(1, "ai", False), (1, "inferred_human", True),
                 (2, "ai", False), (2, "human", False),
                 (3, "human", False), (4, "ai", True), (6, "ai", False)]
        for mid, (cid, attribution, private) in enumerate(cases, 1):
            db.add(MessageEvent(id=mid, conversation_state_id=cid, chatwoot_message_id=mid,
                                direction="outgoing", attribution=attribution, private=private))
        db.add(MessageEvent(id=100, conversation_state_id=5, chatwoot_message_id=100,
                            direction="incoming", created_at=old))
        db.flush()
        # Neither an old legacy reply nor sandbox failures count in this window.
        db.add(AiRun(conversation_state_id=5, trigger_message_id=100, status="completed", action="reply", created_at=old))
        for sid, cid, environment in [(1, 1, "live"), (2, 4, "playground"), (3, 6, "live")]:
            db.add(AutomationSession(id=sid, owner_id=1, conversation_state_id=cid,
                                     inbox_binding_id=2 if cid == 6 else 1, environment=environment))
        db.flush()
        for rid, sid, status, created in [(1, 1, "failed", now), (2, 1, "cancelled", now),
                                          (3, 1, "failed", old), (4, 2, "failed", now),
                                          (5, 3, "failed", now)]:
            db.add(AutomationRun(session_id=sid, module="reply", generation=rid,
                                  idempotency_key=f"metrics-{rid}", status=status, created_at=created))
        db.commit()
    overview = client.get("/v1/bi/overview?inbox_id=10").json()
    assert overview["ai_handled"] == 2
    assert overview["ai_only"] == 1
    assert overview["mixed"] == 1
    assert overview["human_only"] == 1
    assert overview["ai_errors"] == 1
    assert overview["outgoing_messages"] == 4
    assert client.get("/v1/bi/overview?inbox_id=20").json()["ai_errors"] == 1
    assert client.get("/v1/bi/overview").json()["ai_handled"] == 3
    assert client.get("/v1/bi/funnel").json()["items"][1]["value"] == 3
    trends = client.get("/v1/bi/trends").json()["items"]
    assert sum(day["ai"] for day in trends) == 3
    assert sum(day["human"] for day in trends) == 2
    with session_factory() as db:
        db.add(User(id=2, email="scoped@example.com", display_name="Scoped", role="supervisor",
                    password_hash=hash_password("password123")))
        db.flush()
        db.add(UserInboxScope(user_id=2, inbox_binding_id=1))
        db.commit()
    assert client.post("/v1/auth/login", json={"email": "scoped@example.com", "password": "password123"}).status_code == 200
    scoped = client.get("/v1/bi/overview").json()
    assert scoped["ai_handled"] == 2 and scoped["ai_errors"] == 1
    denied = client.get("/v1/bi/overview?inbox_id=20").json()
    assert denied["ai_handled"] == denied["ai_errors"] == 0
