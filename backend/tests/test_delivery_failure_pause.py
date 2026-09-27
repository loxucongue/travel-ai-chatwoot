from sqlalchemy import select
import pytest
from copy import deepcopy

from app.delivery_status import apply_receipt
from app.live_reply_models import LiveReplyJob
from app.models import AiRun, ConversationState, HandoffTask, OutboundMessage, MaterialAsset
from app.lead_capture_models import LeadCaptureState
from app.delivery_tracking import attach_delivery_item
from app.material_library import resolve_materials
from app.deepseek_evaluation import EvaluationDecision
from app.route_reply import journey_for, route_snapshot_from_values
from test_live_reply import setup, add_image, add_itinerary_progress
import app.live_reply as live


def test_late_failed_receipt_blocks_queued_reply_and_creates_one_task(session_factory, monkeypatch):
    setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        out = OutboundMessage(
            conversation_state_id=1, idempotency_key="live:test:failed",
            content="test", source_type="ai", status="submitted", chatwoot_message_id=900,
        )
        db.add(out)
        db.flush()
        assert apply_receipt(db, out, "failed") is True
        db.commit()
        assert db.get(LiveReplyJob, 1).status == "blocked"
        assert db.get(ConversationState, 1).effective_ai_state == "HUMAN_HANDOFF"
        assert out.error_code == "channel_send_failed"
        assert apply_receipt(db, out, "failed") is False
        assert len(db.scalars(select(HandoffTask)).all()) == 1


def test_late_failure_cannot_downgrade_read_receipt(session_factory, monkeypatch):
    setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        out = OutboundMessage(
            conversation_state_id=1, idempotency_key="live:test:read",
            content="test", source_type="ai", status="read", chatwoot_message_id=901,
        )
        db.add(out)
        db.flush()
        assert apply_receipt(db, out, "failed") is False
        assert out.status == "read"
        assert not db.scalars(select(HandoffTask)).all()


def test_submission_receipt_does_not_override_failure(session_factory, monkeypatch):
    setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        out = OutboundMessage(
            conversation_state_id=1, idempotency_key="live:test:stale",
            content="test", source_type="sop", status="failed", chatwoot_message_id=902,
        )
        db.add(out)
        db.flush()
        assert apply_receipt(db, out, "sent") is False
        assert out.status == "failed"


def test_restart_uses_persisted_plan_without_regenerating_or_repeating_text(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    monkeypatch.setattr(live, "generate_decision", lambda _: (_ for _ in ()).throw(AssertionError("must not regenerate")))
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        job.status = "processing"
        job.decision = {"action": "reply", "route_variant": "", "covered_content_groups": []}
        job.trace = {"delivery_plan": [
            {"item_id": "a", "kind": "text", "content": "already submitted", "interval_seconds": 0},
            {"item_id": "b", "kind": "text", "content": "final question", "is_follow_up": True, "interval_seconds": 2},
        ]}
        out = OutboundMessage(conversation_state_id=1, idempotency_key="live:1:text", content="already submitted",
                              content_type="text", source_type="ai", source_id=job.id,
                              status="submitted", chatwoot_message_id=900)
        attach_delivery_item(db, out, journey_for(db, db.get(ConversationState, 1)), [], item_id="a")
        db.add(out)
        db.commit()
        live.recover_jobs(db)
        assert job.status == "queued"
    live.process_job(1)
    assert fake.sent == [("text", "final question")]
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == "submitted"


@pytest.mark.parametrize("change", ["item_id", "content", "conversation", "source_id", "media_id", "route"])
@pytest.mark.parametrize("recover", [False, True])
def test_persisted_identity_mismatch_blocks_before_any_send(session_factory, monkeypatch, change, recover):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        job = db.get(LiveReplyJob, 1)
        job.status = "processing"
        job.decision = {"route_variant": "", "covered_content_groups": []}
        # The mismatch is on the later part: preflight must prevent the body too.
        job.trace = {"delivery_plan": [
            {"item_id": "body", "kind": "text", "content": "Body"},
            {"item_id": "question", "kind": "text", "content": "Question?", "is_follow_up": True},
        ]}
        out = OutboundMessage(conversation_state_id=1, idempotency_key="live:1:follow-up",
                              content="Question?", content_type="text", source_type="ai", source_id=1,
                              status="submitted", chatwoot_message_id=900)
        attach_delivery_item(db, out, journey_for(db, state), [], item_id="question")
        if change == "content":
            out.content = "Different question?"
        elif change == "conversation":
            db.add(ConversationState(id=2, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27))
            db.flush()
            out.conversation_state_id = 2
        elif change == "source_id":
            out.source_id = 2
        else:
            out.content_attributes = {**out.content_attributes, "_delivery_item": {
                **out.content_attributes["_delivery_item"], change: "mismatch",
            }}
        db.add(out)
        db.commit()
        if recover:
            live.recover_jobs(db)
            assert job.status == "blocked"
            assert job.error_code == "delivery_item_identity_mismatch"
        else:
            with pytest.raises(live.ReplyBlocked, match="delivery_item_identity_mismatch"):
                live._execute_persisted_reply(db, fake, job, state, None)
        assert fake.sent == []


def _media_plan(db, tmp_path, *, contact=False):
    asset_id = add_image(db, tmp_path)
    add_itinerary_progress(db)
    state = db.get(ConversationState, 1)
    journey = journey_for(db, state)
    info = resolve_materials(db, ["routes12-9d-itinerary"], journey.route_variant, 1,
                             snapshot=route_snapshot_from_values(journey.route_variant, journey.slots))[0]
    job = db.get(LiveReplyJob, 1)
    job.status = "processing"
    job.decision = {"action": "reply", "route_variant": journey.route_variant,
                    "covered_content_groups": ["itinerary_overview"]}
    body = {"item_id": "body", "kind": "text", "content": "Please leave your LINE."}
    media = {"item_id": "image", "kind": "media", "content": "", "material": info}
    job.trace = {"contact_request_in_plan": contact, "delivery_plan": [body, media] if contact else [media]}
    run = AiRun(conversation_state_id=1, trigger_message_id=1)
    db.add(run)
    if contact:
        db.add(LeadCaptureState(conversation_state_id=1))
    db.commit()
    return job, state, run, asset_id


def test_recovered_media_rechecks_revoked_live_approval(session_factory, monkeypatch, tmp_path):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    monkeypatch.setattr(live, "generate_decision", lambda _: pytest.fail("must reuse persisted plan"))
    with session_factory() as db:
        job, state, run, asset_id = _media_plan(db, tmp_path)
        asset = db.get(MaterialAsset, asset_id)
        asset.metadata_json = {**asset.metadata_json, "live_approved": False}
        db.commit()
        live.recover_jobs(db)
        assert job.status == "queued"
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).error_code == "material_not_approved_for_live"
        assert not db.scalar(select(OutboundMessage))


@pytest.mark.parametrize("failure", [None, "text", "image"])
def test_text_then_assets_contact_count_tracks_successful_body(session_factory, monkeypatch, tmp_path, failure):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    if failure == "text":
        create = fake.create_text_message
        monkeypatch.setattr(fake, "create_text_message", lambda *args: {**create(*args), "status": "failed"})
    elif failure == "image":
        fake.remove_ai_after_text = True
    with session_factory() as db:
        job, state, run, _ = _media_plan(db, tmp_path, contact=True)
        if failure:
            with pytest.raises(live.ReplyBlocked):
                live._execute_persisted_reply(db, fake, job, state, run)
        else:
            live._execute_persisted_reply(db, fake, job, state, run)
        capture = db.scalar(select(LeadCaptureState))
        assert capture.request_count == (0 if failure == "text" else 1)
        assert capture.status == ("not_started" if failure == "text" else "asked")
        assert [kind for kind, _ in fake.sent] == (["text", "image"] if failure is None else ["text"])
        if failure is None:
            live._execute_persisted_reply(db, fake, job, state, run)
            assert capture.request_count == 1
            assert len(fake.sent) == 2


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_terminal_handoff_records_each_attempt_without_enabling_retry(
    session_factory, monkeypatch, tmp_path, captured, unknown,
):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        add_image(db, tmp_path)
        add_itinerary_progress(db)
    if captured:
        fake.messages[0]["content"] = "微信: wx_test_2027"
    decision = EvaluationDecision(
        "handoff", "peach_9d", "contact" if captured else "other",
        reply="A specialist will help you.", route_variant="peach_9d_2027",
        handoff_reason="lead_captured" if captured else "large_group_custom_quote",
        lead_action="captured" if captured else "none",
        contact_values={"wechat": "wx_test_2027"} if captured else {},
        material_keys=["routes12-9d-itinerary"], covered_content_groups=["itinerary_overview"],
    )
    monkeypatch.setattr(live, "generate_decision", lambda _: (decision, [], "hash", {}))
    fake.fail_after_submission = unknown
    live.process_job(1)
    assert [kind for kind, _ in fake.sent] == (["text"] if unknown else ["text", "image"])
    assert fake.operations.index("set_labels") < fake.operations.index("send_text")
    for labels in fake.send_label_snapshots:
        assert "ai" in {label.casefold() for label in labels}
        assert "人工接管" in labels
        if captured:
            assert "已留资" in labels
    assert "ai" not in {label.casefold() for label in fake.labels}
    with session_factory() as db:
        rows = db.scalars(select(OutboundMessage).order_by(OutboundMessage.id)).all()
        assert len(rows) == (1 if unknown else 2)
        job = db.get(LiveReplyJob, 1)
        for out in rows:
            item = out.content_attributes["_delivery_item"]
            part = next(p for p in job.trace['delivery_plan'] if p['item_id'] == item['item_id'])
            assert live._persisted_reply_key(job, part) == out.idempotency_key
            assert item["route"] == "peach_9d_2027"
            assert item["content_hash"]
            assert item["group_keys"] == ["itinerary_overview"]
        assert rows[0].content_attributes["_delivery_item"]["text_group_keys"] == []
        if not unknown:
            assert rows[1].content_attributes["_delivery_item"]["asset_hash"]
        job = db.get(LiveReplyJob, 1)
        assert job.status == ("submission_unknown" if unknown else "handoff")
        # Simulate interruption after the durable send attempt, before completion.
        job.status = "processing"
        db.commit()
        live.recover_jobs(db)
        assert job.status == ("submission_unknown" if unknown else "queued")
        assert bool(job.trace.get("resume_delivery_plan")) is (not unknown)
    before = list(fake.sent)
    live.process_job(1)
    assert fake.sent == before


def _opening_decision(db, tmp_path):
    from test_opening_media import media
    return EvaluationDecision(
        "reply", "unclassified", "other", reply="Hello",
        opening_items=[
            {"key": "hello", "content_type": "text", "content": "Hello"},
            {"key": "repeat", "content_type": "text", "content": "Hello"},
            media(db, tmp_path), media(db, tmp_path, "video"),
            {"key": "question", "content_type": "text", "content": "Which route would you like?"},
        ],
        opening_interval_seconds=4, reply_options=["Route A", "Route B"],
    )


@pytest.mark.parametrize("stop_after", [1, 2, 3, 4, 5])
def test_opening_restart_reuses_frozen_configuration_and_sends_only_remainder(
    session_factory, monkeypatch, tmp_path, stop_after,
):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    waits = []
    monkeypatch.setattr(live.time, "sleep", waits.append)
    with session_factory() as db:
        decision = _opening_decision(db, tmp_path)
    monkeypatch.setattr(live, "generate_decision", lambda _: (decision, [], "hash", {}))
    submit = live.submit_part
    count = 0

    def interrupt(*args, **kwargs):
        nonlocal count
        out = submit(*args, **kwargs)
        count += 1
        if count == stop_after:
            raise SystemExit("restart after durable submission")
        return out

    monkeypatch.setattr(live, "submit_part", interrupt)
    with pytest.raises(SystemExit):
        live.process_job(1)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        frozen = deepcopy(job.trace["delivery_plan"])
        assert job.trace["delivery_plan_format"] == "opening-v1"
        assert [part["interval_seconds"] for part in frozen] == [0, 4, 4, 4, 4]
        assert all(not part["quick_replies"] for part in frozen[:-1])
        assert frozen[-1]["quick_replies"] == ["Route A", "Route B"]
        assert len({part["item_id"] for part in frozen}) == 5
        assert all(part["material"]["media_hash"] for part in frozen if part["kind"] == "media")
        live.recover_jobs(db)
        assert job.status == "queued"
    decision.opening_items.clear()
    decision.reply_options[:] = ["Changed"]
    decision.opening_interval_seconds = 99
    monkeypatch.setattr(live, "generate_decision", lambda _: pytest.fail("must not regenerate opening"))
    monkeypatch.setattr(live, "submit_part", submit)
    live.process_job(1)
    assert [part[0] for part in fake.sent] == ["text", "text", "image", "image", "input_select"]
    assert fake.sent[-1][2] == ["Route A", "Route B"]
    assert waits == [4, 4, 4, 4]
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "submitted" and job.trace["outbound"]
        assert [part["item_id"] for part in job.trace["delivery_plan"]] == [part["item_id"] for part in frozen]
        assert all(part["status"] == "submitted" for part in job.trace["delivery_plan"])
        rows = db.scalars(select(OutboundMessage).order_by(OutboundMessage.id)).all()
        assert [out.content_type for out in rows] == ["text", "text", "image", "video", "input_select"]
        assert len({out.idempotency_key for out in rows}) == 5
        assert all(":opening-v1:" in out.idempotency_key for out in rows)
        assert all("_opening_delivery" in out.content_attributes for out in rows)
    before = list(fake.sent)
    live.process_job(1)
    assert fake.sent == before


@pytest.mark.parametrize("change", ["missing", "bytes", "tenant", "type"])
def test_opening_media_is_revalidated_before_resumed_send(session_factory, monkeypatch, tmp_path, change):
    from app.models import StoredMedia, Tenant
    from pathlib import Path
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        decision = _opening_decision(db, tmp_path)
        state = db.get(ConversationState, 1)
        job = db.get(LiveReplyJob, 1)
        job.status = "processing"
        job.decision = {"action": "reply", "route_variant": "", "covered_content_groups": []}
        live._freeze_opening_reply(db, job, state, decision)
        db.commit()
        row = db.get(StoredMedia, decision.opening_items[2]["media_id"])
        if change == "missing":
            Path(row.storage_path).unlink()
        elif change == "bytes":
            from PIL import Image
            Image.new("RGB", (32, 32), "red").save(row.storage_path, format="PNG")
        elif change == "tenant":
            # Ownership must be checked even when the pinned hash still matches.
            db.add(Tenant(id=2, name="Other"))
            db.flush()
            row.tenant_id = 2
        else:
            row.media_type = "video"
        db.commit()
        live.recover_jobs(db)
    monkeypatch.setattr(live, "generate_decision", lambda _: pytest.fail("must not regenerate opening"))
    live.process_job(1)
    assert not any(part[0] in {"image", "input_select"} for part in fake.sent)
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status in {"failed", "blocked"}


def test_opening_validates_all_media_before_first_submission(session_factory, monkeypatch, tmp_path):
    from pathlib import Path
    from app.models import StoredMedia
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        decision = _opening_decision(db, tmp_path)
        Path(db.get(StoredMedia, decision.opening_items[-2]["media_id"]).storage_path).unlink()
    monkeypatch.setattr(live, "generate_decision", lambda _: (decision, [], "hash", {}))
    live.process_job(1)
    assert fake.sent == []


@pytest.mark.parametrize("change", ["options", "interval", "content"])
def test_opening_frozen_envelope_tampering_blocks_recovery(session_factory, monkeypatch, tmp_path, change):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        job.status = "processing"
        live._freeze_opening_reply(db, job, db.get(ConversationState, 1), _opening_decision(db, tmp_path))
        plan = deepcopy(job.trace["delivery_plan"])
        field = {"options": "quick_replies", "interval": "interval_seconds", "content": "content"}[change]
        plan[-1][field] = ["Changed"] if change == "options" else 99 if change == "interval" else "Changed"
        job.trace = {**job.trace, "delivery_plan": plan}
        db.commit()
        live.recover_jobs(db)
        assert job.status == "blocked"
        assert job.error_code == "delivery_item_identity_mismatch"
    assert fake.sent == []


def test_legacy_opening_without_plan_remains_unknown(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        job.status = "processing"
        db.add(OutboundMessage(conversation_state_id=1, idempotency_key="live:1:opening:0",
                               source_type="ai", source_id=1, content="Hello", status="submitted"))
        db.commit()
        live.recover_jobs(db)
        assert job.status == "submission_unknown"
    live.process_job(1)
    assert fake.sent == []


def test_opening_options_receipt_identity_must_match_frozen_selection(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    live.process_job(1)
    before = list(fake.sent)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "submitted"
        out = db.scalar(select(OutboundMessage))
        assert out.content_type == "input_select"
        out.content_attributes = {**out.content_attributes, "items": [{"title": "Changed", "value": "Changed"}]}
        job.status = "processing"
        db.commit()
        live.recover_jobs(db)
        assert job.status == "blocked"
        assert job.error_code == "delivery_item_identity_mismatch"
    live.process_job(1)
    assert fake.sent == before


def test_opening_freezes_existing_media_skip_without_dropping_final_options(session_factory, monkeypatch, tmp_path):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        decision = _opening_decision(db, tmp_path)
        image_id = decision.opening_items[2]["media_id"]
        image_hash = decision.opening_items[2]["media_hash"]
        db.add(OutboundMessage(conversation_state_id=1, idempotency_key="earlier:opening:image",
                               source_type="ai", source_id=99, content="", status="submitted", media_id=image_id,
                               content_type="image", content_attributes={"_delivery_item": {"asset_hash": image_hash}}))
        db.commit()
    monkeypatch.setattr(live, "generate_decision", lambda _: (decision, [], "hash", {}))
    live.process_job(1)
    assert [part[0] for part in fake.sent] == ["text", "text", "image", "input_select"]
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "submitted"
        assert job.trace["delivery_plan"][2]["skip_previously_sent"]
        assert job.trace["delivery_plan"][2]["status"] == "previously_sent"
        assert job.trace["delivery_plan"][-1]["quick_replies"] == ["Route A", "Route B"]
