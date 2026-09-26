import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.automation_models import AutomationSession, AutomationRun, MaterialDelivery, RehearsalJob
from app.automation_service import (process_automation_run, confirm_draft, sop_snapshot,
    enroll_rehearsal, advance_sops, subject_key, reply_policy, enrollment_allowed, dt)
from app.models import KnowledgeVersion, MaterialAsset, StoredMedia, InboxBinding, SopDefinition, OutboundMessage
from app.material_library import (CATALOG_VERSION, material_info, candidate_materials,
    resolve_materials, record_delivery, previous_delivery, same_content_group)
from app.deepseek_evaluation import EvaluationDecision
from app.decision_service import generate_decision
from app.route_reply import prepare_route_reply_values, bind_new_route_snapshot, route_snapshot_from_values

NOW = "2026-08-26T02:00:00+00:00"
ROUTE = "peach_9d_2027"


def new_route_journey():
    _, journey = prepare_route_reply_values(EvaluationDecision(
        action="reply", branch="peach_9d", intent="other", route_variant=ROUTE,
    ))
    return journey


@pytest.mark.parametrize("month", ["9月", "九月", "11月", "十二月"])
def test_model_handles_out_of_scope_month_without_handoff_or_material(month):
    decision = EvaluationDecision(action="reply", branch="peach_9d", intent="departure",
        reply=f"{month}不在目前頁面參考區間內，暫時無法確認該時段安排。", slots={"departure_window": month},
        slot_evidence={"departure_window": month}, route_variant=ROUTE,
        evidence_refs=["route.9.departure"])
    result, _, _, _ = generate_decision({"customer_text": month, "context_messages": [
        {"direction": "incoming", "content": "想了解桃花9日，不上珠峰"}],
        "route_variant": ROUTE, "journey": new_route_journey(), "available_materials": [{"key": "photo"}]},
        model_call=lambda _: (decision, [], "hash"))
    assert result.action == "reply"
    assert result.handoff_reason is None
    assert result.material_keys == [] and result.route_variant == ROUTE
    assert result.slots["departure_window"] == month


@pytest.mark.parametrize("text", ["想了解9日行程", "3月", "4月"])
def test_reference_month_or_duration_does_not_block_general_intro(text):
    decision = EvaluationDecision(action="reply", branch="peach_9d", intent="route_intro",
        reply="這是桃花9日路線的參考資料。", route_variant=ROUTE, evidence_refs=["route.9.overview"])
    result, _, _, _ = generate_decision({"customer_text": text, "context_messages": [], "route_variant": ROUTE,
        "journey": new_route_journey()},
        model_call=lambda _: (decision, [], "hash"))
    assert result.action == "reply"


def catalog(db, tmp_path):
    inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="test")
    version = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION, title="test", content_hash="x" * 64)
    db.add_all([inbox, version]); db.flush()
    assets = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        path.write_bytes(f"image-{index}".encode())
        media = StoredMedia(tenant_id=1, original_name=path.name, media_type="image", mime_type="image/png", file_size=path.stat().st_size, storage_path=str(path), created_by=1)
        db.add(media); db.flush()
        asset_key = "routes12-9d-itinerary" if index == 0 else f"image-{index}"
        asset = MaterialAsset(knowledge_version_id=version.id, asset_key=asset_key, source_path=str(path), display_name=f"Photo {index}", available=True,
            file_hash=hashlib.sha256(path.read_bytes()).hexdigest(), metadata_json={"stored_media_id":media.id, "route_variants":[ROUTE],
                "content_family":"room" if index < 2 else "car", "review_state":"evaluation_ready"})
        db.add(asset); assets.append(asset)
    db.flush()
    return inbox, assets


def session(db, inbox, *, new_reception=False):
    row = AutomationSession(owner_id=1, inbox_binding_id=inbox.id, mode="sop", virtual_now=NOW, generation=1,
        controls={"can_reply":True, "ai_enabled":True, "channel":"facebook", "route_variant":ROUTE, "customer_added_at":NOW,
            "journey":{"stage":"collecting_departure", "slots":{"party_size":2}, "sent_content_groups":["entry_question"]}},
        messages=[{"direction":"incoming", "content":"桃花9日有住宿照片嗎？", "created_at":NOW}])
    if new_reception:
        slots = bind_new_route_snapshot(ROUTE, {"party_size": 2}, available_materials=candidate_materials(db, 1))
        snapshot = route_snapshot_from_values(ROUTE, slots)
        assert snapshot is not None and snapshot["asset_bindings"]["routes12-9d-itinerary"]
        row.controls = {**row.controls, "journey": {
            "route_variant": ROUTE, "stage": "collecting_departure",
            "slots": slots, "sent_content_groups": [],
        }}
    db.add(row); db.flush()
    return row


def item(asset):
    return {"key":"photo", "content_type":"image", "media_id":asset.metadata_json["stored_media_id"], "asset_key":asset.asset_key}


def strategy(db, asset):
    sop = SopDefinition(tenant_id=1, created_by=1, name="pilot", status="running", route_variant=ROUTE, test_conversation_ids=[26],
        nodes=[{"key":"room", "schedule_type":"relative", "basis":"enrollment", "delay_minutes":10,"skip_if_materials_provided":True,
            "messages":[{"key":"text", "content_type":"text", "content":"Room reference"},item(asset)]},
            {"key":"next", "schedule_type":"relative", "basis":"previous_node", "delay_minutes":20,
                "messages":[{"key":"text", "content_type":"text", "content":"Next"}]}])
    db.add(sop); db.flush()
    return sop_snapshot(db,sop,1)


def test_route_review_hash_and_tenant_gates(session_factory, tmp_path):
    with session_factory() as db:
        _, assets = catalog(db,tmp_path)
        a = assets[0]
        info = material_info(db,item(a),ROUTE,1)
        assert info["media_hash"] == a.file_hash
        by_asset_key = material_info(
            db,
            {"content_type": "image", "asset_key": a.asset_key},
            ROUTE,
            1,
        )
        assert by_asset_key["media_id"] == a.metadata_json["stored_media_id"]
        assert by_asset_key["media_hash"] == a.file_hash
        for route, tenant, reason in [("peach_11d_2027",1,"material_route_mismatch"),(ROUTE,2,"material_unavailable")]:
            with pytest.raises(ValueError,match=reason):material_info(db,item(a),route,tenant)
        a.metadata_json = {**a.metadata_json,"review_state":"pending_review"}
        with pytest.raises(ValueError,match="material_review_required"):material_info(db,item(a),ROUTE,1)
        assert len(candidate_materials(db,1)) == 2 and candidate_materials(db,None) == []
        a.metadata_json = {**a.metadata_json,"review_state":"evaluation_ready"}
        (tmp_path/"0.png").write_bytes(b"changed")
        with pytest.raises(ValueError,match="material_revision_changed"):material_info(db,{**item(a),"media_hash":info["media_hash"]},ROUTE,1)


def test_unselected_route_can_only_resolve_configured_overview_visual(session_factory, tmp_path):
    with session_factory() as db:
        _, assets = catalog(db, tmp_path)
        overview = resolve_materials(db, [assets[0].asset_key], "", 1)
        assert len(overview) == 1
        assert overview[0]["route_variant"] == ROUTE
        with pytest.raises(ValueError, match="material_route_mismatch"):
            resolve_materials(db, [assets[2].asset_key], "", 1)


def test_distinct_hashes_in_same_family_send_but_duplicate_upload_deduplicates(session_factory,tmp_path):
    with session_factory() as db:
        inbox, assets=catalog(db,tmp_path)
        s=session(db,inbox)
        snapshot = {
            "asset_bindings": {asset.asset_key: item(asset) for asset in assets[:2]},
            "asset_hashes": {asset.asset_key: asset.file_hash for asset in assets[:2]},
        }
        infos=resolve_materials(db,[assets[0].asset_key,assets[1].asset_key],ROUTE,1, snapshot=snapshot)
        assert len(infos)==2
        assert infos[0]["content_family"] == infos[1]["content_family"]
        assert infos[0]["media_hash"] != infos[1]["media_hash"]
        assert record_delivery(db,s,subject_key(db,s),infos[0],"ai:1","ai",ROUTE)
        second=material_info(db,item(assets[1]),ROUTE,1)
        assert previous_delivery(db,subject_key(db,s),second,include_group=True) is None
        assert record_delivery(db,s,subject_key(db,s),second,"sop:1","sop",ROUTE)
        source=db.get(StoredMedia,infos[0]["media_id"])
        duplicate=StoredMedia(tenant_id=1,original_name="renamed.png",media_type="image",mime_type="image/png",file_size=source.file_size,storage_path=source.storage_path,created_by=1)
        db.add(duplicate); db.flush()
        duplicate_info = material_info(db,{"media_id":duplicate.id,"content_type":"image"},ROUTE,1)
        assert duplicate_info["media_id"] != infos[0]["media_id"]
        assert duplicate_info["media_hash"] == infos[0]["media_hash"]
        duplicate_info = {**duplicate_info, "content_family": "renamed-family",
                          "content_group_key": f"{ROUTE}:different-topic"}
        assert previous_delivery(db,subject_key(db,s),duplicate_info)
        assert not record_delivery(db,s,subject_key(db,s),duplicate_info,"sop:duplicate","sop",ROUTE)
        assert len(db.scalars(select(MaterialDelivery)).all()) == 2


def test_semantic_group_dedup_is_scoped_to_route(session_factory, tmp_path):
    with session_factory() as db:
        _, assets = catalog(db, tmp_path)
        legacy_asset = assets[0]
        legacy_asset.metadata_json = {
            **legacy_asset.metadata_json,
            "content_family": "legacy_hotel_photo",
            "content_group_key": "hotel_reference",
            "route_variants": [ROUTE],
        }
        new_file_same_group = {
            "content_family": "website_hilton_room",
            "content_group_key": f"{ROUTE}:hotel_reference",
        }
        other_route_itinerary = {
            "content_family": "website_11d_itinerary",
            "content_group_key": "peach_11d_2027:itinerary_overview",
        }

        assert same_content_group(legacy_asset, new_file_same_group)
        assert not same_content_group(legacy_asset, other_route_itinerary)


def test_ai_confirm_then_sop_skips_exact_image_keeps_text_and_dependency(session_factory,tmp_path,monkeypatch):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path)
        s=session(db,inbox,new_reception=True)
        _,pv=reply_policy(db,inbox.id)
        run=AutomationRun(session_id=s.id,module="reply",generation=s.generation,policy_version=pv,idempotency_key="ai",input_snapshot={"customer_text":"桃花9日住宿照片", "context_messages":[]})
        db.add(run); db.commit()
        decision=EvaluationDecision(action="reply",branch="peach_9d",intent="itinerary",reply="提供客房參考。",route_variant=ROUTE,material_keys=[assets[0].asset_key])
        monkeypatch.setattr("app.automation_service.generate_decision",lambda payload:(decision,[],"x",{}))
        assert process_automation_run(db)
        db.refresh(s)
        assert [m.get("content_type") for m in s.messages[-2:]]==["text","image"]
        confirm_draft(db,s,f"draft:{run.id}")
        confirm_draft(db,s,f"draft:{run.id}")
        assert len(db.scalars(select(MaterialDelivery)).all())==1
        assert all(x["status"]=="simulated_delivered" for x in s.messages[-2:])
        v=strategy(db,assets[0]); enroll_rehearsal(db,s,v)
        jobs=db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        s.virtual_now=jobs[0].scheduled_at; advance_sops(db,s)
        assert jobs[0].status=="simulated_delivered" and jobs[0].confirmed_at is not None
        assert dt(jobs[1].scheduled_at) == dt(jobs[0].confirmed_at) + timedelta(minutes=20)
        assert len(s.messages)==4
        assert s.messages[-1]["content"] == "Room reference"
        assert not s.messages[-1].get("media_id")
        assert sum(bool(message.get("media_id")) and message.get("status") == "simulated_delivered"
                   for message in s.messages) == 1
        assert len(db.scalars(select(MaterialDelivery)).all()) == 1
        s.virtual_now=jobs[1].scheduled_at; advance_sops(db,s)
        assert jobs[1].status=="simulated_delivered" and jobs[1].reason is None
        assert not db.scalars(select(OutboundMessage)).all()


def test_sop_first_ai_later_does_not_repeat_image(session_factory,tmp_path):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path); s=session(db,inbox,new_reception=True)
        v=strategy(db,assets[0]); enroll_rehearsal(db,s,v)
        s.virtual_now="2026-08-26T02:10:00+00:00"; advance_sops(db,s)
        _,pv=reply_policy(db,inbox.id)
        run=AutomationRun(session_id=s.id,module="reply",generation=s.generation,policy_version=pv,idempotency_key="after-sop",status="completed",decision={"action":"reply","route_variant":ROUTE})
        db.add(run);db.flush()
        s.messages=[*s.messages,{"id":f"draft:{run.id}","run_id":run.id,"status":"draft","direction":"outgoing","content":"photo"},
            {**item(assets[0]),"id":f"draft:{run.id}:media:0","run_id":run.id,"status":"draft","direction":"outgoing"}]
        confirm_draft(db,s,f"draft:{run.id}")
        assert s.messages[-1]["status"]=="already_provided"
        assert s.messages[-2]["content"] == "photo"
        assert len(db.scalars(select(MaterialDelivery)).all())==1


def test_route_change_stops_sop(session_factory,tmp_path):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path); s=session(db,inbox)
        v=strategy(db,assets[0]); enroll_rehearsal(db,s,v)
        s.controls={**s.controls,"route_variant":"peach_11d_2027"}
        s.virtual_now="2026-08-26T02:10:00+00:00";advance_sops(db,s)
        assert db.scalar(select(RehearsalJob)).reason=="material_route_mismatch"
        assert not db.scalars(select(MaterialDelivery)).all()


@pytest.mark.parametrize("reference_only", [True, False])
def test_different_hash_same_purpose_delivers_image_and_text(session_factory,tmp_path,reference_only):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path); s=session(db,inbox)
        for a in (assets[0],assets[2]):a.metadata_json={**a.metadata_json,"content_group_key":"hotel_intro"}
        first=material_info(db,item(assets[0]),ROUTE,1)
        second=material_info(db,item(assets[2]),ROUTE,1)
        assert first["content_group_key"] == second["content_group_key"]
        assert first["media_hash"] != second["media_hash"]
        record_delivery(db,s,subject_key(db,s),first,"ai:first","ai",ROUTE)
        assert previous_delivery(db,subject_key(db,s),second,include_group=True) is None
        v=strategy(db,assets[2])
        if not reference_only:v.config={**v.config,"nodes":[{**n,"skip_if_materials_provided":False} for n in v.config["nodes"]]}
        enroll_rehearsal(db,s,v)
        s.virtual_now="2026-08-26T02:10:00+00:00";advance_sops(db,s)
        assert db.scalar(select(RehearsalJob)).status == "simulated_delivered"
        assert len(s.messages)==3
        assert s.messages[-2]["content"] == "Room reference"
        assert s.messages[-1]["media_id"] == second["media_id"]
        assert all(message["status"] == "simulated_delivered" for message in s.messages[1:])
        assert {row.asset_hash for row in db.scalars(select(MaterialDelivery)).all()} == {
            first["media_hash"], second["media_hash"],
        }
        assert db.scalar(select(OutboundMessage)) is None


def test_exact_hash_blocks_whole_sop_when_skip_is_disabled(session_factory, tmp_path):
    with session_factory() as db:
        inbox, assets = catalog(db, tmp_path)
        s = session(db, inbox)
        info = material_info(db, item(assets[0]), ROUTE, 1)
        assert record_delivery(db, s, subject_key(db, s), info, "ai:first", "ai", ROUTE)
        version = strategy(db, assets[0])
        version.config = {**version.config, "nodes": [
            {**node, "skip_if_materials_provided": False} for node in version.config["nodes"]
        ]}
        enroll_rehearsal(db, s, version)
        s.virtual_now = "2026-08-26T02:10:00+00:00"
        advance_sops(db, s)
        jobs = db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        assert jobs[0].status == "blocked"
        assert jobs[0].reason == "material_already_provided"
        assert jobs[0].confirmed_at is None
        assert jobs[1].reason == "predecessor_not_confirmed"
        assert len(s.messages) == 1
        assert len(db.scalars(select(MaterialDelivery)).all()) == 1
        assert db.scalar(select(OutboundMessage)) is None


def test_changed_file_after_publication_is_not_sent(session_factory,tmp_path):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path); s=session(db,inbox)
        v=strategy(db,assets[0]);enroll_rehearsal(db,s,v)
        (tmp_path/"0.png").write_bytes(b"replaced")
        s.virtual_now="2026-08-26T02:10:00+00:00";advance_sops(db,s)
        assert db.scalar(select(RehearsalJob)).reason=="material_revision_changed"
        assert len(s.messages)==1


def test_explicit_resend_records_an_exception_once(session_factory,tmp_path):
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path);s=session(db,inbox,new_reception=True)
        info=material_info(db,item(assets[0]),ROUTE,1)
        record_delivery(db,s,subject_key(db,s),info,"sop:1","sop",ROUTE)
        _,pv=reply_policy(db,inbox.id)
        run=AutomationRun(session_id=s.id,module="reply",generation=s.generation,policy_version=pv,idempotency_key="resend",status="completed",
            input_snapshot={"customer_text":"請再發一次圖片"},decision={"action":"reply","route_variant":ROUTE,"allow_material_resend":True})
        db.add(run);db.flush()
        s.messages=[*s.messages,{"id":f"draft:{run.id}","run_id":run.id,"status":"draft","content":"參考圖", "direction":"outgoing"},
            {**item(assets[0]),"id":"photo","run_id":run.id,"status":"draft","direction":"outgoing"}]
        confirm_draft(db,s,f"draft:{run.id}");confirm_draft(db,s,f"draft:{run.id}")
        assert s.messages[-1]["status"]=="simulated_delivered"
        assert len(db.scalars(select(MaterialDelivery)).all())==2


def test_whitelist_uses_remote_conversation_id(session_factory,tmp_path):
    from app.models import ConversationState
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path);s=session(db,inbox)
        conversation=ConversationState(tenant_id=1,inbox_binding_id=inbox.id,chatwoot_conversation_id=27)
        db.add(conversation);db.flush();s.conversation_state_id=conversation.id
        config={"test_conversation_ids":[26],"inbox_ids":[128859]}
        assert not enrollment_allowed(db,s,config)
        conversation.chatwoot_conversation_id=26
        assert enrollment_allowed(db,s,config)


def test_library_api_is_scoped_and_has_no_paths(authenticated,session_factory,tmp_path):
    client,csrf=authenticated
    with session_factory() as db:
        inbox,assets=catalog(db,tmp_path);db.commit(); iid=inbox.id;aid=assets[0].id;mid=assets[0].metadata_json["stored_media_id"]
    result=client.get(f"/v1/materials?inbox_binding_id={iid}&route_variant={ROUTE}")
    assert result.status_code==200 and len(result.json()["items"])==3
    assert str(tmp_path) not in result.text and "storage_path" not in result.text
    assert client.get("/v1/materials?inbox_binding_id=999").status_code==403
    assert client.get(f"/v1/media/{mid}/preview").status_code==200
    assert client.post(f"/v1/knowledge/assets/{aid}/bind",json={"media_id":mid},headers={"X-CSRF-Token":csrf}).status_code==409


def test_route_branch_binding_is_enforced_by_model_schema():
    with pytest.raises(ValueError, match="deepseek_route_binding_missing"):
        EvaluationDecision.parse({
            "action": "reply",
            "branch": "peach_11d",
            "intent": "route_intro",
            "reply": "參考圖",
            "route_variant": "peach_9d_2027",
        })


def test_route_month_meaning_is_left_to_model_not_reparsed_by_code():
    decision = EvaluationDecision(
        action="reply",
        branch="unclassified",
        intent="departure",
        reply="9月不在目前頁面參考區間內。",
        route_variant="",
        route_evidence="9月",
    )
    result, *_ = generate_decision(
        {"customer_text": "9月想去西藏看圖", "context_messages": [], "available_materials": []},
        lambda _payload: (decision, [], "x"),
    )
    assert result.route_variant == ""
    assert result.action == "reply"
