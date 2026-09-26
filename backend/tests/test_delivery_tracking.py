from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from app.delivery_tracking import (
    ASSET_BINDINGS_KEY, DELIVERY_ITEM_KEY, attach_delivery_item,
    find_existing_delivery, pin_route_assets, record_delivery_progress, refresh_delivery_progress,
)
from app.models import ConversationJourney, ConversationState, OutboundMessage, StoredMedia
from app.route_reply import (
    CONTENT_PROGRESS_KEY, ROUTE_SNAPSHOTS_KEY, _snapshot_digest,
    prepare_route_reply_values, route_snapshot_from_values,
)

ROUTE = "peach_9d_2027"


@pytest.fixture
def tracking(session_factory):
    with session_factory() as db:
        state = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=101)
        db.add(state)
        db.flush()
        _, prepared = prepare_route_reply_values(SimpleNamespace(route_variant=ROUTE))
        journey = ConversationJourney(conversation_state_id=state.id, route_variant=ROUTE,
                                      slots=prepared["slots"], sent_groups=[])
        db.add(journey)
        db.flush()
        yield db, journey


def outbound(db, journey, content, status="submitted", **kwargs):
    import uuid
    out = OutboundMessage(conversation_state_id=journey.conversation_state_id,
                          idempotency_key=str(uuid.uuid4()), content=content,
                          status=status, content_type="text", **kwargs)
    db.add(out)
    db.flush()
    return out


def text_group(journey):
    return next((key, group) for key, group in route_snapshot_from_values(
        ROUTE, journey.slots)["groups"].items() if group.get("text") and not group.get("assets"))


@pytest.mark.parametrize('engine', ['v1', 'v2'])
def test_answer_memory_follows_persisted_job_and_confirmed_receipt(tracking, engine):
    from app.live_reply_models import LiveReplyJob
    from app.models import MessageEvent
    from app.reception_v2.events import merge_events, validate_events
    db, journey = tracking
    message = MessageEvent(conversation_state_id=journey.conversation_state_id,
                           chatwoot_message_id=99, direction='incoming', content='多少钱')
    db.add(message)
    db.flush()
    events = validate_events([{'type': 'question', 'quote': '多少钱'}],
                             {'customer_text': message.content, 'source_message_id': message.id})
    journey.slots = merge_events(journey.slots, events)
    job = LiveReplyJob(conversation_state_id=journey.conversation_state_id,
        trigger_message_id=message.id, engine_version=engine, due_at='2026-09-20T00:00:00+00:00',
        decision={'action': 'reply', 'reply': '人民币9980元起', 'route_variant': ROUTE,
                  'evidence_refs': ['route.9.price'], 'v2_events': events})
    db.add(job)
    db.flush()
    out = outbound(db, journey, job.decision['reply'], source_type='ai', source_id=job.id)
    attach_delivery_item(db, out, journey, ['price_reference'])
    db.commit()
    db.expire_all()
    refresh_delivery_progress(db, journey)
    assert journey.slots['_v2_state']['questions'][0]['status'] == 'pending'
    out.status = 'delivered'
    refresh_delivery_progress(db, journey)
    state = journey.slots['_v2_state']
    assert state['questions'][0]['status'] == ('answer_provided' if engine == 'v2' else 'pending')
    assert bool(state['provided_fact_ids']) == (engine == 'v2')
    out.status = 'failed'
    refresh_delivery_progress(db, journey)
    assert journey.slots['_v2_state']['questions'][0]['status'] == 'pending'
    assert not journey.slots['_v2_state']['provided_fact_ids']


@pytest.mark.parametrize("status,complete", [("submitted", False), ("planned", False),
    ("submission_unknown", False), ("sent", True), ("delivered", True), ("read", True), ("failed", False)])
def test_receipts_only(tracking, status, complete):
    db, journey = tracking
    key, spec = text_group(journey)
    out = outbound(db, journey, spec["text"], status)
    attach_delivery_item(db, out, journey, [key])
    result = record_delivery_progress(db, out)
    assert (key in journey.sent_groups) is complete
    assert result[key]["text_delivered"] is complete
    assert result[key]["history_unknown"] is (status == "submission_unknown")
    assert out.status == status


def test_exact_text_topic_and_failure_recomputation(tracking):
    db, journey = tracking
    key, spec = text_group(journey)
    paraphrase = outbound(db, journey, spec["text"] + " extra", "sent")
    attach_delivery_item(db, paraphrase, journey, [key])
    progress = record_delivery_progress(db, paraphrase)
    assert progress[key]["topic_covered"] and not journey.sent_groups
    exact = outbound(db, journey, spec["text"], "read")
    attach_delivery_item(db, exact, journey, [key])
    record_delivery_progress(db, exact)
    assert key in journey.sent_groups
    exact.status = "failed"
    progress = record_delivery_progress(db, exact)
    assert not journey.sent_groups and progress[key]["topic_covered"]
    paraphrase.status = "failed"
    assert not record_delivery_progress(db, paraphrase)[key]["topic_covered"]
    version = journey.version
    refresh_delivery_progress(db, journey)
    assert journey.version == version


def test_immutable_json_roundtrip_and_payload_tamper(tracking):
    db, journey = tracking
    key, spec = text_group(journey)
    out = outbound(db, journey, spec["text"], "sent", content_attributes={"items": [1]})
    item = attach_delivery_item(db, out, journey, [key], plan_version="p1", item_id="i1")
    item["group_keys"].clear()
    assert out.content_attributes[DELIVERY_ITEM_KEY]["group_keys"] == [key]
    assert attach_delivery_item(db, out, journey, [key], plan_version="p1", item_id="i1")
    with pytest.raises(ValueError, match="immutable"):
        attach_delivery_item(db, out, journey, [key], plan_version="p2")
    db.commit()
    db.expire_all()
    assert out.content_attributes["items"] == [1]
    assert out.content_attributes["delivery_item"] == {
        "group_keys": [key], "route": ROUTE, "plan_version": "p1", "item_id": "i1", "asset_key": ""}
    out.content += " changed"
    assert record_delivery_progress(db, out)[key]["history_unknown"]
    assert not journey.sent_groups


def test_unknown_snapshot_never_falls_back(tracking):
    db, journey = tracking
    key, spec = text_group(journey)
    journey.slots = {}
    journey.sent_groups = [key]
    out = outbound(db, journey, spec["text"], "sent")
    assert attach_delivery_item(db, out, journey, [key])["history_unknown"]
    assert record_delivery_progress(db, out)[key]["history_unknown"]
    assert not journey.sent_groups


def test_route_switch_and_duplicate_contributions(tracking):
    db, journey = tracking
    key, spec = text_group(journey)
    for _ in range(2):
        out = outbound(db, journey, spec["text"], "sent")
        attach_delivery_item(db, out, journey, [key])
    out.status = "failed"
    record_delivery_progress(db, out)
    assert key in journey.sent_groups
    journey.route_variant = "peach_11d_2027"
    journey.sent_groups = []
    record_delivery_progress(db, out)
    assert not journey.sent_groups
    assert journey.slots[CONTENT_PROGRESS_KEY][ROUTE][key]["text_delivered"]


def media_item(db, journey, tmp_path):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"approved bytes")
    media = StoredMedia(tenant_id=1, original_name="asset.bin", media_type="image",
                        mime_type="image/png", file_size=14, storage_path=str(path), created_by=1)
    db.add(media)
    db.flush()
    key, spec = next((key, group) for key, group in route_snapshot_from_values(
        ROUTE, journey.slots)["groups"].items() if group.get("assets"))
    out = outbound(db, journey, "", "sent", media_id=media.id)
    out.content_type = "image"
    return out, key, spec["assets"][0], path


def test_new_asset_pins_approved_hash_and_rejects_drift(tracking, tmp_path, monkeypatch):
    db, journey = tracking
    out, key, asset_key, path = media_item(db, journey, tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    approved = SimpleNamespace(asset_key=asset_key, available=True, file_hash=digest,
        metadata_json={"live_approved": True, "review_state": "evaluation_ready", "route_variants": [ROUTE]})
    monkeypatch.setattr("app.material_library.catalog_assets", lambda *args: [approved])
    attach_delivery_item(db, out, journey, [key], asset_key)
    assert journey.slots[ASSET_BINDINGS_KEY][ROUTE][asset_key] == digest
    assert record_delivery_progress(db, out)[key]["asset_keys"] == [asset_key]
    path.write_bytes(b"changed")
    other = outbound(db, journey, "", "submitted", media_id=out.media_id)
    other.content_type = "image"
    with pytest.raises(ValueError, match="hash_mismatch"):
        attach_delivery_item(db, other, journey, [key], asset_key)
    out.status = "failed"
    assert record_delivery_progress(db, out)[key]["asset_keys"] == []


def test_bound_snapshot_hash_does_not_use_current_catalog(tracking, tmp_path, monkeypatch):
    db, journey = tracking
    out, key, asset_key, path = media_item(db, journey, tmp_path)
    slots = deepcopy(journey.slots)
    entry = slots[ROUTE_SNAPSHOTS_KEY][ROUTE]
    entry["spec"]["asset_hashes"] = {asset_key: hashlib.sha256(path.read_bytes()).hexdigest()}
    entry["digest"] = _snapshot_digest(entry["spec"])
    journey.slots = slots
    monkeypatch.setattr("app.material_library.catalog_assets", lambda *args: [])
    attach_delivery_item(db, out, journey, [key], asset_key)
    assert record_delivery_progress(db, out)[key]["asset_keys"] == [asset_key]


def test_legacy_history_cannot_bind_new_asset(tracking, tmp_path):
    db, journey = tracking
    out, key, asset_key, _ = media_item(db, journey, tmp_path)
    journey.sent_groups = [key]
    with pytest.raises(ValueError, match="history_unknown"):
        attach_delivery_item(db, out, journey, [key], asset_key)


def test_cross_conversation_rejected(tracking):
    db, journey = tracking
    out = outbound(db, journey, "test")
    out.conversation_state_id += 10
    with pytest.raises(ValueError, match="journey_mismatch"):
        attach_delivery_item(db, out, journey, [])


def test_pin_all_assets_before_text_then_later_hotel(tracking, tmp_path, monkeypatch):
    db, journey = tracking
    out, group, asset_key, path = media_item(db, journey, tmp_path)
    media_id = out.media_id
    db.delete(out)
    db.flush()
    required = {key for spec in route_snapshot_from_values(ROUTE, journey.slots)["groups"].values()
                for key in spec.get("assets", [])}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = [SimpleNamespace(asset_key=key, available=True, file_hash=digest,
        metadata_json={"live_approved": True, "review_state": "evaluation_ready",
                       "route_variants": [ROUTE], "stored_media_id": media_id}) for key in required]
    monkeypatch.setattr("app.material_library.catalog_assets", lambda *args: catalog)
    pinned = pin_route_assets(db, journey, 1)
    assert pinned == dict.fromkeys(required, digest)
    text_key, text_spec = text_group(journey)
    text = outbound(db, journey, text_spec["text"], "sent")
    attach_delivery_item(db, text, journey, [text_key])
    record_delivery_progress(db, text)
    monkeypatch.setattr("app.material_library.catalog_assets", lambda *args: [])
    assert pin_route_assets(db, journey, 1) == pinned
    image = outbound(db, journey, "", "delivered", media_id=media_id)
    image.content_type = "image"
    attach_delivery_item(db, image, journey, [group], asset_key)
    assert record_delivery_progress(db, image)[group]["asset_keys"] == [asset_key]


def test_pin_is_atomic_and_rejects_history(tracking, monkeypatch):
    db, journey = tracking
    before = deepcopy(journey.slots)
    monkeypatch.setattr("app.material_library.catalog_assets", lambda *args: [])
    with pytest.raises(ValueError, match="not_approved"):
        pin_route_assets(db, journey, 1)
    assert journey.slots == before
    outbound(db, journey, "legacy", "sent")
    with pytest.raises(ValueError, match="history_unknown"):
        pin_route_assets(db, journey, 1)


def candidate(journey, content, **kwargs):
    return OutboundMessage(conversation_state_id=journey.conversation_state_id,
                           idempotency_key="reenrolled", content=content,
                           content_type="text", status="submission_unknown", **kwargs)


def test_snapshot_hashes_pin_after_unselected_opening(tracking, tmp_path, monkeypatch):
    db, journey = tracking
    outbound(db, journey, "Which route interests you?", "sent")
    image, key, asset_key, path = media_item(db, journey, tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    slots = deepcopy(journey.slots)
    snapshot = slots[ROUTE_SNAPSHOTS_KEY][ROUTE]
    required = {asset for group in snapshot["spec"]["groups"].values()
                for asset in group.get("assets", [])}
    snapshot["spec"]["asset_hashes"] = dict.fromkeys(required, digest)
    snapshot["spec"]["asset_bindings"] = {asset: {
        "asset_key": asset, "media_hash": digest, "media_id": image.media_id,
        "content_type": "image",
    } for asset in required}
    snapshot["digest"] = _snapshot_digest(snapshot["spec"])
    journey.slots = slots

    def no_catalog(*args):
        pytest.fail("Complete snapshot hashes must not consult today's catalog")

    monkeypatch.setattr("app.material_library.catalog_assets", no_catalog)
    assert pin_route_assets(db, journey, 1) == dict.fromkeys(required, digest)
    assert journey.slots[ASSET_BINDINGS_KEY][ROUTE] == dict.fromkeys(required, digest)
    assert journey.slots[ROUTE_SNAPSHOTS_KEY] == slots[ROUTE_SNAPSHOTS_KEY]
    assert pin_route_assets(db, journey, 1) == dict.fromkeys(required, digest)
    # Binding does not certify current bytes; attachment still rejects drift.
    path.write_bytes(b"changed after model snapshot")
    with pytest.raises(ValueError, match="delivery_asset_hash_mismatch"):
        attach_delivery_item(db, image, journey, [key], asset_key)


@pytest.mark.parametrize("problem", ["invalid_hash", "conflicting_pin", "conflicting_reference"])
def test_snapshot_hash_pin_rejects_invalid_or_conflicting_identity(tracking, problem):
    db, journey = tracking
    slots = deepcopy(journey.slots)
    snapshot = slots[ROUTE_SNAPSHOTS_KEY][ROUTE]
    required = {asset for group in snapshot["spec"]["groups"].values()
                for asset in group.get("assets", [])}
    key = sorted(required)[0]
    snapshot["spec"]["asset_hashes"] = dict.fromkeys(required, "a" * 64)
    if problem == "invalid_hash":
        snapshot["spec"]["asset_hashes"][key] = "not-a-sha256"
    elif problem == "conflicting_pin":
        slots[ASSET_BINDINGS_KEY] = {ROUTE: {key: "b" * 64}}
    else:
        snapshot["spec"]["asset_bindings"] = {key: {"asset_key": key, "media_hash": "b" * 64}}
    snapshot["digest"] = _snapshot_digest(snapshot["spec"])
    journey.slots = slots
    before = deepcopy(slots)
    with pytest.raises(ValueError, match="delivery_asset_hash_"):
        pin_route_assets(db, journey, 1)
    assert journey.slots == before


@pytest.mark.parametrize("status", ["submitted", "sent", "delivered", "read"])
def test_find_reviewed_text_reuses_original_without_insert(tracking, status):
    from sqlalchemy import func, select
    db, journey = tracking
    key, spec = text_group(journey)
    old = outbound(db, journey, spec["text"], status)
    attach_delivery_item(db, old, journey, [key], plan_version="old", item_id="old")
    db.flush()
    new = candidate(journey, spec["text"])
    attach_delivery_item(db, new, journey, [key], plan_version="reenrolled", item_id="new")
    # Even a caller that has already added the row must not trigger autoflush.
    db.add(new)
    assert find_existing_delivery(db, new, journey) is old
    assert new.id is None
    db.expunge(new)
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 1
    record_delivery_progress(db, old)
    assert (key in journey.sent_groups) is (status != "submitted")


@pytest.mark.parametrize("status,error", [("submission_unknown", "submission_unknown_reconcile_required"),
    ("unknown", "submission_unknown_reconcile_required"), ("failed", "channel_send_failed")])
def test_find_blocks_unknown_and_failed_even_with_success(tracking, status, error):
    db, journey = tracking
    key, spec = text_group(journey)
    for value in ["read", status]:
        old = outbound(db, journey, spec["text"], value)
        attach_delivery_item(db, old, journey, [key])
    db.flush()
    new = candidate(journey, spec["text"])
    attach_delivery_item(db, new, journey, [key])
    with pytest.raises(ValueError, match=f"^{error}$"):
        find_existing_delivery(db, new, journey)


@pytest.mark.parametrize("change", ["generated", "snapshot", "route", "group", "conversation", "tampered", "planned"])
def test_find_does_not_reuse_unrelated_or_unapproved_items(tracking, change):
    db, journey = tracking
    key, spec = text_group(journey)
    body = "generated body" if change == "generated" else spec["text"]
    old = outbound(db, journey, body, "planned" if change == "planned" else "sent")
    attach_delivery_item(db, old, journey, [key])
    attrs = deepcopy(old.content_attributes)
    if change in {"snapshot", "route", "group"}:
        field = {"snapshot": "snapshot_digest", "route": "route", "group": "group_keys"}[change]
        attrs[DELIVERY_ITEM_KEY][field] = ["different"] if change == "group" else "different"
        old.content_attributes = attrs
    if change == "conversation":
        old.conversation_state_id += 1
    if change == "tampered":
        old.content += " tampered"
    db.flush()
    new = candidate(journey, body)
    attach_delivery_item(db, new, journey, [key])
    assert find_existing_delivery(db, new, journey) is None


def test_find_asset_uses_bound_hash_not_media_row_id(tracking, tmp_path):
    db, journey = tracking
    old, key, asset_key, path = media_item(db, journey, tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    slots = deepcopy(journey.slots)
    slots[ASSET_BINDINGS_KEY] = {ROUTE: {asset_key: digest}}
    journey.slots = slots
    attach_delivery_item(db, old, journey, [key], asset_key)
    media = StoredMedia(tenant_id=1, original_name="copy.bin", media_type="image",
                        mime_type="image/png", file_size=14, storage_path=str(path), created_by=1)
    db.add(media)
    db.flush()
    new = candidate(journey, "", media_id=media.id)
    new.content_type = "image"
    attach_delivery_item(db, new, journey, [key], asset_key)
    assert find_existing_delivery(db, new, journey) is old
    attrs = deepcopy(old.content_attributes)
    attrs[DELIVERY_ITEM_KEY]["asset_hash"] = "different"
    old.content_attributes = attrs
    db.flush()
    assert find_existing_delivery(db, new, journey) is None


@pytest.mark.parametrize("status", ["sent", "delivered", "read", "failed"])
def test_receipt_syncs_reply_trace_and_public_status(tracking, status):
    from app.live_reply_models import LiveReplyJob
    from app.models import utcnow
    db, journey = tracking
    key, spec = text_group(journey)
    job = LiveReplyJob(conversation_state_id=journey.conversation_state_id,
                       trigger_message_id=900, due_at=utcnow(), trace={"other": "keep"})
    db.add(job)
    db.flush()
    out = outbound(db, journey, spec["text"], "submitted", source_id=job.id)
    attach_delivery_item(db, out, journey, [key], item_id="part-1")
    private = deepcopy(out.content_attributes[DELIVERY_ITEM_KEY])
    job.trace = {"other": "keep", "delivery_plan": [
        {"item_id": "part-1", "status": "submitted"},
        {"item_id": "different", "outbound_id": out.id, "status": "submitted"},
        {"item_id": "part-1", "outbound_id": out.id + 100, "status": "submitted"},
    ]}
    unrelated = LiveReplyJob(conversation_state_id=journey.conversation_state_id,
        trigger_message_id=901, due_at=utcnow(), trace={"delivery_plan": [
            {"item_id": "part-1", "status": "submitted"}]})
    db.add(unrelated)
    out.status = status
    record_delivery_progress(db, out)
    db.commit()
    db.expire_all()
    assert job.trace["other"] == "keep"
    assert [part["status"] for part in job.trace["delivery_plan"]] == [status, status, "submitted"]
    assert all(part["outbound_id"] == out.id for part in job.trace["delivery_plan"][:2])
    assert unrelated.trace["delivery_plan"][0]["status"] == "submitted"
    assert out.content_attributes["delivery_item"]["status"] == status
    assert out.content_attributes[DELIVERY_ITEM_KEY] == private
