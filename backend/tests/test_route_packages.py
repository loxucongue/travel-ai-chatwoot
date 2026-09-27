import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from sqlalchemy import select

from app.deepseek_evaluation import EvaluationDecision
from app.config import settings
from app.models import KnowledgeVersion, MaterialAsset, SopDefinition, StoredMedia
from app.route_packages import (
    JOURNEY_POLICY, PACKAGE_ROOT, ROUTE_PACKAGES, RoutePackageError, _runtime_journey_sop, _validate,
)
from app.reception_config import configured_silence_nodes
from app.route_reply import append_deferred_initial_follow_up, deferred_initial_follow_up
from app.silence_touch_pipeline import SILENCE_TOUCH_PROMPT_VERSION


def test_follow_up_is_appended_after_all_immediate_mainline_nodes():
    decision = EvaluationDecision(
        action="reply",
        branch="peach_11d",
        intent="route_intro",
        route_variant="peach_11d_2027",
        follow_up_type="slot",
        follow_up_field="party_size",
        follow_up_question="這次大概會有幾位一起來呢？",
    )
    deferred = deferred_initial_follow_up(decision, ["itinerary_overview"])
    nodes = [
        {"key": "initial_highlights", "initial_delivery": True},
        {"key": "initial_hotel", "initial_delivery": True},
        {"key": "initial_vehicle", "initial_delivery": True},
        {"key": "silence_mainline", "journey_trigger": "silence_mainline"},
    ]

    result = append_deferred_initial_follow_up(nodes, decision.route_variant, deferred)

    assert [node["key"] for node in result] == [
        "initial_highlights",
        "initial_hotel",
        "initial_vehicle",
        "initial_delivery_follow_up",
        "silence_mainline",
    ]
    assert result[3]["messages"][0]["content"] == decision.follow_up_question
    assert result[3]["basis"] == "previous_node"


def test_follow_up_is_not_deferred_without_a_remaining_immediate_group():
    decision = EvaluationDecision(
        action="reply",
        branch="peach_9d",
        intent="route_intro",
        route_variant="peach_9d_2027",
        follow_up_question="這次大概會有幾位一起來呢？",
    )
    route = ROUTE_PACKAGES[decision.route_variant]
    sent = [
        key for key in route["content_sequence"]
        if route["content_groups"][key].get("initial_delivery") is True
    ]

    assert deferred_initial_follow_up(decision, sent) is None
    original = [{"key": "silence_mainline", "journey_trigger": "silence_mainline"}]
    assert append_deferred_initial_follow_up(original, decision.route_variant, {
        "question": decision.follow_up_question,
    }) == original


def test_only_two_reviewed_products_are_enabled():
    assert set(ROUTE_PACKAGES) == {"peach_9d_2027", "peach_11d_2027"}
    assert len(ROUTE_PACKAGES["peach_9d_2027"]["sop"]["nodes"]) == 10
    assert len(ROUTE_PACKAGES["peach_11d_2027"]["sop"]["nodes"]) == 11
    assert "不去珠峰" in ROUTE_PACKAGES["peach_9d_2027"]["match_keywords"]
    assert "珠峰大本營" in ROUTE_PACKAGES["peach_11d_2027"]["match_keywords"]
    assert all(
        package["initial_delivery_interval_seconds"] == 2
        for package in ROUTE_PACKAGES.values()
    )


def test_route_package_compiles_configured_initial_delivery_interval():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    package["initial_delivery_interval_seconds"] = 7
    from app.route_packages import _runtime_journey_sop

    runtime = _runtime_journey_sop(package, JOURNEY_POLICY)
    initial = [node for node in runtime["nodes"] if node.get("initial_delivery") is True]
    assert initial[0]["delay_seconds"] == 0
    assert all(node["delivery_interval_seconds"] == 7 for node in initial)
    assert all(node["delay_seconds"] == 7 for node in initial[1:])


def test_initial_delivery_respects_all_delivery_modes():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    groups = package["content_groups"]
    groups["advisor_greeting"]["delivery_mode"] = "text_only"
    groups["brand_positioning"]["delivery_mode"] = "text_only"
    groups["peach_highlights"]["delivery_mode"] = "text_then_assets"
    groups["itinerary_overview"]["delivery_mode"] = "assets_then_text"
    groups["hotel_reference"]["delivery_mode"] = "assets_only"
    runtime = _runtime_journey_sop(package, JOURNEY_POLICY)
    nodes = {node["content_group_key"]: node for node in runtime["nodes"] if node.get("initial_delivery")}
    assert [item["content_type"] for item in nodes["advisor_greeting"]["messages"]] == ["text"]
    assert [item["content_type"] for item in nodes["peach_highlights"]["messages"]] == ["text", "image", "image"]
    assert [item["content_type"] for item in nodes["itinerary_overview"]["messages"]] == ["image", "text"]
    assert [item["content_type"] for item in nodes["hotel_reference"]["messages"]] == ["image", "image"]


def test_first_wave_order_keeps_hilton_before_rongbuk():
    nine = ROUTE_PACKAGES["peach_9d_2027"]
    eleven = ROUTE_PACKAGES["peach_11d_2027"]
    assert nine["content_sequence"][:6] == [
        "advisor_greeting", "brand_positioning", "itinerary_overview",
        "peach_highlights", "hotel_reference", "vehicle_reference",
    ]
    assert eleven["content_sequence"].index("hotel_reference") < eleven["content_sequence"].index("rongbuk_reference")
    assert eleven["content_sequence"].index("rongbuk_reference") < eleven["content_sequence"].index("vehicle_reference")


def test_route_package_rejects_invalid_initial_delivery_interval():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    package["initial_delivery_interval_seconds"] = 0
    with pytest.raises(RoutePackageError, match="initial_delivery_interval_invalid"):
        _validate(package, Path("test-route-package.json"))


def test_route_packages_are_bound_to_the_saved_website_snapshot():
    manifest_path = PACKAGE_ROOT.parent / "website-7693-full" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_html = manifest_path.parent / "source" / "content.html"
    assert hashlib.sha256(source_html.read_bytes()).hexdigest() == manifest["raw_html_sha256"]
    for package in ROUTE_PACKAGES.values():
        assert package["source"]["snapshot_sha256"] == manifest["raw_html_sha256"]
        assert (PACKAGE_ROOT.parents[3] / package["source"]["branch_content"]).is_file()


def test_route_material_manifest_uses_high_resolution_originals():
    manifest_path = PACKAGE_ROOT.parent / "routes-1-2" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    images = [image for route in manifest["routes"] for image in route["images"]]
    assert images
    assert all("/hires-" in f"/{image['path']}" for image in images)
    assert all(max(image["width"], image["height"]) >= 744 for image in images)
    assert {image["mime_type"] for image in images} <= {"image/jpeg", "image/png"}


def test_route_package_preserves_operator_copy_without_style_gate():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    package["content_groups"]["hotel_reference"]["approved_text"] = (
        "客戶詢問設備時再發對應實景。"
    )
    validated = _validate(package, Path("test-route-package.json"))
    assert validated['content_groups']['hotel_reference']['approved_text'] == '客戶詢問設備時再發對應實景。'


def test_route_package_preserves_fixed_answer_without_word_substitution():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    package["fixed_answers"][0]["answer_text"] = "我先把酒店照片發您看。"
    package["fixed_answers"][0]["answer_origin"] = "operator_approved"
    validated = _validate(package, Path("test-route-package.json"))
    assert validated['fixed_answers'][0]['answer_text'] == '我先把酒店照片發您看。'


def test_website_fixed_answers_are_single_source_verbatim_copy():
    knowledge_root = PACKAGE_ROOT.parent
    for package in ROUTE_PACKAGES.values():
        for answer in package["fixed_answers"]:
            assert answer["status"] in {"active", "pending_review", "disabled"}
            assert "enabled" not in answer
            assert "review_state" not in answer
            assert "source_text" not in answer
            if answer["answer_origin"] != "website_verbatim":
                continue
            source_path = knowledge_root / answer["source_ref"].split("#", 1)[0]
            website = source_path.read_text(encoding="utf-8")
            assert all(line in website for line in answer["answer_text"].splitlines())


def test_high_risk_website_copy_cannot_hide_inside_an_active_general_answer():
    for package in ROUTE_PACKAGES.values():
        answers = {answer["id"]: answer for answer in package["fixed_answers"]}
        assert answers["hotel"]["status"] == "active"
        assert "85-90%" not in answers["hotel"]["answer_text"]
        assert answers["vehicle"]["status"] == "pending_review"
        for answer_id in {
            "no_shopping_contract_claim",
            "senior_health_document",
            "age_75_entry_claim",
            "young_child_suitability",
            "ticket_refund_claim",
            "hotel_oxygen_concentration_claim",
            "mobile_oxygen_cabin_claim",
            "vehicle_model_year_claim",
        }:
            assert answers[answer_id]["status"] == "pending_review"


def test_legacy_fixed_answer_flags_are_normalized_to_one_status():
    package = deepcopy(ROUTE_PACKAGES["peach_9d_2027"])
    package.pop("source_path", None)
    package.pop("runtime_sop", None)
    answer = next(item for item in package["fixed_answers"] if item["answer_origin"] == "website_verbatim")
    answer.pop("status")
    answer.pop("answer_origin")
    answer["enabled"] = True
    answer["review_state"] = "approved"
    checked = _validate(package, Path("legacy-route-package.json"))
    normalized = next(item for item in checked["fixed_answers"] if item["id"] == answer["id"])
    assert normalized["status"] == "active"
    assert normalized["answer_origin"] == "website_verbatim"
    assert "enabled" not in normalized
    assert "review_state" not in normalized


def test_default_route_sops_wait_for_customer_silence_before_first_followup():
    for package in ROUTE_PACKAGES.values():
        first = package["sop"]["nodes"][0]
        assert first["basis"] == "enrollment"
        assert first["delay_minutes"] == 10
        assert first["skip_if_slots_present"] == ["party_size"]
        departure = next(node for node in package["sop"]["nodes"] if node["key"] == "departure_question")
        assert departure["skip_if_slots_present"] == ["departure_window"]


def test_runtime_journey_compiles_customer_silence_intervals():
    assert JOURNEY_POLICY["reply_style"]["max_characters"] == 200
    assert JOURNEY_POLICY["reply_style"]["max_questions_per_turn"] == 1
    assert JOURNEY_POLICY["handoff"]["large_group"]["minimum_party_size"] == 8
    for package in ROUTE_PACKAGES.values():
        runtime = package["runtime_sop"]
        initial = [node for node in runtime["nodes"] if node.get("initial_delivery") is True]
        silence = [node for node in runtime["nodes"] if node.get("initial_delivery") is not True]
        assert runtime["journey_policy_version"] == JOURNEY_POLICY["policy_version"]
        assert [node["delay_minutes"] for node in silence] == [1, 3, 5, 10, 30, 60]
        assert [node["basis"] for node in silence] == [
            "enrollment", "previous_node", "previous_node", "previous_node", "previous_node", "previous_node"
        ]
        assert [node["journey_trigger"] for node in silence] == [
            "silence_mainline", "wakeup", "wakeup", "wakeup", "wakeup", "wakeup"
        ]
        assert [node["content_group_key"] for node in initial] == [
            key for key in package["content_sequence"]
            if package["content_groups"][key].get("initial_delivery") is True
        ]
        assert all(node["delay_minutes"] == 0 for node in initial)
        assert [node["delay_seconds"] for node in initial] == [
            0,
            *([2] * (len(initial) - 1)),
        ]
        assert all(node["delivery_interval_seconds"] == 2 for node in initial)
        for node in silence:
            candidate_keys = {item["content_group_key"] for item in node["content_group_candidates"]}
            assert candidate_keys == set(package["content_sequence"]) - {"advisor_greeting", "brand_positioning"}


def test_disabling_silence_keeps_only_checked_initial_delivery_nodes():
    package = ROUTE_PACKAGES["peach_9d_2027"]
    nodes = configured_silence_nodes(
        package["runtime_sop"]["nodes"],
        [1, 3, 5],
        silence_enabled=False,
    )
    assert nodes
    assert all(node.get("initial_delivery") is True for node in nodes)
    assert [node["content_group_key"] for node in nodes] == [
        key for key in package["content_sequence"]
        if package["content_groups"][key].get("initial_delivery") is True
    ]


def test_human_followup_style_comes_from_real_chat_analysis():
    style = JOURNEY_POLICY["human_followup_style"]
    assert style["evidence_summary"]["silence_followups"] == 316
    assert style["evidence_summary"]["generic_checkin_observed_response_rate"] < (
        style["evidence_summary"]["single_need_question_observed_response_rate"]
    )
    assert len(style["touch_progression"]) == 6
    assert style["touch_progression"][0]["touch_index"] == 1
    assert style["touch_progression"][-1]["touch_index"] == 6
    assert "行程看了嗎" in style["forbidden_default_openers"]


def test_route_reference_copy_is_conversational_not_generic_read_check():
    for package in ROUTE_PACKAGES.values():
        groups = package["content_groups"]
        read_check = groups["read_check"]["approved_text"]
        assert "有看到了嗎" not in read_check
        assert "還滿意嗎" not in read_check
        assert "頁面展示" not in groups["hotel_reference"]["approved_text"]
        assert "頁面提供" not in groups["vehicle_reference"]["approved_text"]
        assert "完整行程" in groups["contact_request"]["approved_text"]
        assert "不確定時不再追問" not in groups["contact_request"]["approved_text"]
        assert "邀請客戶" not in groups["contact_request"]["approved_text"]


def test_hotel_copy_matches_the_room_and_oxygen_images_sent_as_one_group():
    for package in ROUTE_PACKAGES.values():
        hotel = package["content_groups"]["hotel_reference"]
        assert hotel["asset_keys"] == ["routes12-hilton-room", "routes12-hilton-oxygen"]
        assert "希爾頓" in hotel["approved_text"] and "客房" in hotel["approved_text"]
        assert "供氧設備" in hotel["approved_text"]
        assert "客戶詢問" not in hotel["approved_text"]
        node = next(item for item in package["sop"]["nodes"] if item["key"] == "hotel_reference")
        text = next(item["content"] for item in node["messages"] if item.get("content_type", "text") == "text")
        assert text == hotel["approved_text"]


def test_product_policy_does_not_handoff_for_answerable_boundary_questions():
    for package in ROUTE_PACKAGES.values():
        topics = set(package["policies"]["handoff_topics"])
        assert "availability" not in topics
        assert "health_or_permit" not in topics
        assert topics == {
            "lead_captured",
            "explicit_human_request",
            "complaint",
            "refund",
            "contract_dispute",
            "attachment_requires_vision",
        }


def test_every_sop_message_uses_reviewed_route_content_or_asset():
    for package in ROUTE_PACKAGES.values():
        groups = package["content_groups"]
        fact_ids = {fact["id"] for fact in package["knowledge_facts"]} | {
            "service.requirements", "service.safety"
        }
        reviewed_assets = {asset for group in groups.values() for asset in group.get("asset_keys", [])}
        assert all(
            evidence in fact_ids
            for group in groups.values()
            for evidence in group.get("evidence_refs", [])
        )
        for node in package["sop"]["nodes"]:
            assert node["content_group_key"] in groups
            for message in node["messages"]:
                if message.get("content_type", "text") == "text":
                    assert message["content"].strip()
                else:
                    assert message["asset_key"] in reviewed_assets


def test_route_product_api_exposes_complete_read_only_configuration(authenticated):
    client, _csrf = authenticated
    response = client.get("/v1/automation/route-products")
    assert response.status_code == 200
    payload = response.json()
    assert payload["outbound"] is False
    assert {item["route_variant"] for item in payload["items"]} == {
        "peach_9d_2027", "peach_11d_2027"
    }
    for item in payload["items"]:
        assert item["editable"] is True
        assert isinstance(item["ai_guidance"], str)
        assert isinstance(item["match_keywords"], list)
        assert item["match_keywords"]
        assert item["versions"][0]["status"] == "current"
        assert "accommodation_summary" not in item["content_sequence"]
        assert "zhaji" in item["content_sequence"]
        assert "read_check" == item["content_sequence"][-1]
        assert item["source"]["url"] == "https://china2go.com/7693-2/"
        assert item["knowledge_facts"]
        assert item["content_groups"]
        assert item["fixed_answers"]
        assert any(group["initial_delivery"] for group in item["content_groups"])
        assert all(answer["status"] in {"active", "pending_review", "disabled"} for answer in item["fixed_answers"])
        assert all(answer["answer_origin"] in {"website_verbatim", "operator_approved"} for answer in item["fixed_answers"])
        assert all("source_text" not in answer for answer in item["fixed_answers"])
        assert item["policies"]["entry_group"]
        assert item["journey_policy"]["policy_version"] == "2026-09-04.2"
        assert item["journey_policy"]["source"]["message_count"] == 6108
        assert item["default_sop"]["nodes"]
        silence_nodes = [
            node for node in item["default_sop"]["nodes"]
            if node.get("initial_delivery") is not True
        ]
        assert [node["delay_minutes"] for node in silence_nodes] == [1, 3, 5, 10, 30, 60]
        assert item["readiness"]["assets_total"] == len(item["assets"])
        for asset in item["assets"]:
            assert set(asset) >= {
                "what_it_shows", "feature_points", "customer_value",
                "recommended_caption", "avoid_claims",
            }
            assert isinstance(asset["feature_points"], list)
            assert isinstance(asset["avoid_claims"], list)


def test_route_product_simulation_is_read_only_and_reports_sop_preview(authenticated, monkeypatch):
    client, csrf = authenticated

    def fake_generate_decision(packet):
        assert packet["customer_text"] == "我想了解桃花9日行程"
        assert packet["route_variant"] == "peach_9d_2027"
        return EvaluationDecision.parse({
            "action": "reply",
            "branch": "peach_9d",
            "intent": "route_intro",
            "reply": "桃花9日會從林芝賞花開始，我先給您看行程重點。請問您預計幾位同行？",
            "route_variant": "peach_9d_2027",
            "content_group_key": "itinerary_overview",
            "covered_content_groups": ["itinerary_overview", "entry_question"],
            "lead_action": "none",
            "slots": {},
            "slot_evidence": {},
            "evidence_refs": ["route.9.overview"],
        }), [{"status": "completed", "input_tokens": 10, "output_tokens": 20}], "hash", {
            "prompt_version": "split-realtime-reply-v8",
        }

    monkeypatch.setattr("app.automation_api.generate_decision", fake_generate_decision)
    response = client.post(
        "/v1/automation/route-products/peach_9d_2027/simulate",
        headers={"X-CSRF-Token": csrf},
        json={
            "scenario": "normal",
            "messages": [{"role": "customer", "content": "我想了解桃花9日行程"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["outbound"] is False
    assert payload["action"] == "reply"
    assert payload["route_variant"] == "peach_9d_2027"
    assert payload["covered_content_groups"] == ["itinerary_overview", "entry_question"]
    assert payload["next_sop_preview"]["will_enroll"] is True
    assert payload["next_sop_preview"]["next_node"] == "initial_delivery_advisor_greeting"


def test_route_product_simulation_surfaces_handoff_without_sop(authenticated, monkeypatch):
    client, csrf = authenticated

    def fake_generate_decision(_packet):
        return EvaluationDecision.parse({
            "action": "handoff",
            "branch": "peach_9d",
            "intent": "other",
            "reply": "",
            "route_variant": "peach_9d_2027",
            "handoff_reason": "large_group_custom_quote",
            "lead_action": "none",
        }), [{"status": "completed"}], "hash", {
            "prompt_version": "split-realtime-reply-v8",
        }

    monkeypatch.setattr("app.automation_api.generate_decision", fake_generate_decision)
    response = client.post(
        "/v1/automation/route-products/peach_9d_2027/simulate",
        headers={"X-CSRF-Token": csrf},
        json={
            "scenario": "large_group",
            "messages": [{"role": "customer", "content": "我们12个人想包团"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["outbound"] is False
    assert payload["action"] == "handoff"
    assert payload["blocked_reason"] == "large_group_custom_quote"
    assert payload["next_sop_preview"]["will_enroll"] is False


def test_reception_configuration_is_editable_and_returned_with_hard_guards(authenticated, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    initial = client.get("/v1/automation/reception-config")
    assert initial.status_code == 200
    payload = initial.json()
    assert payload["config"]["schema_version"] == 5
    assert payload["model_nodes"] == {
                "advisor_voice": "china2go-taiwan-advisor-voice-v15",
            "customer_understanding": "customer-understanding-v20",
                "business_planner": "deterministic-reply-planner-v35",
                "reply_generation": "planned-reply-generator-v69",
                "reply_fact_verification": "reply-fact-verifier-v16",
        "silence_planner": "deterministic-silence-planner-v13",
        "silence_generation": "planned-silence-generator-v32",
            "silence_touch": SILENCE_TOUCH_PROMPT_VERSION,
    }
    assert {rule["system_key"] for rule in payload["config"]["business_rules"]} >= {
        "large_group", "captured_contact", "explicit_human", "service_dispute", "attachment_review",
    }
    assert payload["config"]["silence"]["intervals_minutes"] == [1, 3, 5, 10, 30, 60]
    assert payload["config"]["stage_journey"] == {
        "mandatory_send": False,
        "skip_when_no_relevant_content": True,
        "model_max_attempts": 2,
        "model_failure_action": "warn_and_skip_touch",
        "stages": [
            "route_selection", "needs_discovery", "value_building", "objection_handling",
            "contact_ready", "contact_requested", "considering", "captured", "handoff",
        ],
        "touch_goals": [
            "route_choice", "collect_need", "build_value", "handle_objection",
            "request_contact", "contact_reminder", "soft_nurture",
        ],
    }
    assert set(payload["config"]["profile_fields"]) >= {
        "first_time_tibet", "permit_awareness", "concerns", "decision_status",
        "intent_level", "unresolved_question", "contact_status",
    }
    assert payload["hard_guards"]["rollout_scope"] == "allowlist"
    assert payload["hard_guards"]["require_ai_label"] is True
    assert payload["hard_guards"]["evaluation_outbound"] is False

    config = payload["config"]
    config["reply"]["goal"] = "先完整回答，再取得一种联系方式"
    config["reply"]["tone_guidance"] = "像台灣真人旅遊顧問，語氣柔和自然。"
    config["reply"]["max_images_per_turn"] = 2
    config["handoff"]["large_group_minimum"] = 15
    config["silence"]["intervals_minutes"] = [2, 4, 8, 16, 32, 64, 128, 256]
    config["silence"]["max_proactive_messages_per_day"] = 8
    config["business_rules"].append({
        "id": "vip_customer",
        "name": "高价值客户优先人工",
        "enabled": True,
        "condition": "客户档案确认是高价值客户",
        "action": "handoff",
        "guidance": "说明将由资深顾问继续接待。",
        "system_key": None,
    })
    updated = client.put(
        "/v1/automation/reception-config",
        headers={"X-CSRF-Token": csrf},
        json=config,
    )
    assert updated.status_code == 200
    assert updated.json()["config"]["reply"]["max_images_per_turn"] == 2
    assert updated.json()["config"]["reply"]["tone_guidance"] == "像台灣真人旅遊顧問，語氣柔和自然。"
    assert updated.json()["config"]["handoff"]["large_group_minimum"] == 15
    assert updated.json()["config"]["business_rules"][-1]["id"] == "vip_customer"
    assert updated.json()["existing_active_sop_rounds_unchanged"] is True

    effective = client.get("/v1/automation/route-products").json()["items"][0]["journey_policy"]
    assert effective["reply_style"]["max_images_per_turn"] == 2
    assert effective["handoff"]["large_group"]["minimum_party_size"] == 15
    assert effective["handoff"]["large_group"]["source"] == "operator_reception_config"
    assert effective["silence_journey"]["mainline_after_minutes"] == 2
    assert effective["silence_journey"]["wakeup_after_minutes"] == [4, 8, 16, 32, 64, 128, 256]
    assert effective["silence_journey"]["wakeup_max_touches"] == 7
    assert effective["silence_journey"]["stop_after_minutes"] == 510
    route_product = client.get("/v1/automation/route-products").json()["items"][0]
    dynamic_nodes = [
        node for node in route_product["default_sop"]["nodes"]
        if node.get("initial_delivery") is not True
    ]
    assert len(dynamic_nodes) == 8
    assert [node["delay_minutes"] for node in dynamic_nodes] == [
        2, 4, 8, 16, 32, 64, 128, 256,
    ]
    assert dynamic_nodes[-1]["key"] == "wakeup_7"
    assert effective["operator_configuration"]["business_rules"][-1]["id"] == "vip_customer"
    assert effective["operator_configuration"]["tone_guidance"] == "像台灣真人旅遊顧問，語氣柔和自然。"


def test_reception_configuration_rejects_more_than_twenty_silence_nodes(authenticated):
    client, csrf = authenticated
    config = client.get("/v1/automation/reception-config").json()["config"]
    config["silence"]["intervals_minutes"] = list(range(1, 22))
    config["silence"]["max_proactive_messages_per_day"] = 20
    response = client.put(
        "/v1/automation/reception-config",
        headers={"X-CSRF-Token": csrf},
        json=config,
    )
    assert response.status_code == 422


def test_route_content_can_be_edited_and_republished(authenticated, monkeypatch):
    import app.automation_api as automation_api

    client, csrf = authenticated
    current = client.get("/v1/automation/route-products").json()["items"][0]
    captured = {}

    def fake_publish(payload, user, db):
        captured["package"] = payload.package
        return {"draft": {"route_variant": payload.package["route_variant"]}, "outbound": False}

    monkeypatch.setattr(automation_api, "import_route_product_draft", fake_publish)
    current["default_entry_message"] = "新的可编辑开场"
    current["ai_guidance"] = "优先说明这条线路的轻松节奏。"
    current["initial_delivery_interval_seconds"] = 6
    current["match_keywords"] = ["九天慢遊", "不去珠峰"]
    current["knowledge_facts"][0]["text"] = "运营更新后的事实"
    current["content_groups"][0]["approved_text"] = "运营更新后的回复内容"
    current["content_groups"][0]["initial_delivery"] = not current["content_groups"][0]["initial_delivery"]
    current["fixed_answers"][0]["answer_text"] = "這是已審核、需要逐字發送的固定回答。"
    response = client.put(
        f"/v1/automation/route-products/{current['route_variant']}/content",
        headers={"X-CSRF-Token": csrf},
        json={
            "name": current["name"],
            "selection_title": current["selection_title"],
            "default_entry_message": current["default_entry_message"],
            "ai_guidance": current["ai_guidance"],
            "initial_delivery_interval_seconds": current["initial_delivery_interval_seconds"],
            "match_keywords": current["match_keywords"],
            "knowledge_facts": current["knowledge_facts"],
            "content_groups": current["content_groups"],
            "content_sequence": list(reversed(current["content_sequence"])),
            "fixed_answers": current["fixed_answers"],
        },
    )
    assert response.status_code == 200, response.text
    assert captured["package"]["default_entry_message"] == "新的可编辑开场"
    assert captured["package"]["ai_guidance"] == "优先说明这条线路的轻松节奏。"
    assert captured["package"]["initial_delivery_interval_seconds"] == 6
    assert captured["package"]["match_keywords"] == ["九天慢遊", "不去珠峰"]
    assert captured["package"]["knowledge_facts"][0]["text"] == "运营更新后的事实"
    assert captured["package"]["content_groups"][current["content_groups"][0]["key"]]["approved_text"] == "运营更新后的回复内容"
    assert captured["package"]["content_groups"][current["content_groups"][0]["key"]]["initial_delivery"] == current["content_groups"][0]["initial_delivery"]
    assert captured["package"]["content_sequence"] == list(reversed(current["content_sequence"]))
    assert captured["package"]["fixed_answers"] == current["fixed_answers"]


def test_route_content_rejects_invalid_mainline(authenticated):
    client, csrf = authenticated
    current = client.get("/v1/automation/route-products").json()["items"][0]
    payload = {
        "name": current["name"],
        "selection_title": current["selection_title"],
        "default_entry_message": current["default_entry_message"],
        "ai_guidance": current["ai_guidance"],
        "knowledge_facts": current["knowledge_facts"],
        "content_groups": current["content_groups"],
        "content_sequence": [current["content_sequence"][0], current["content_sequence"][0]],
    }
    response = client.put(
        f"/v1/automation/route-products/{current['route_variant']}/content",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert response.status_code == 422
    assert "route_content_sequence_duplicate" in response.text


def test_route_content_keeps_mainline_for_cached_older_client(authenticated, monkeypatch):
    import app.automation_api as automation_api

    client, csrf = authenticated
    current = client.get("/v1/automation/route-products").json()["items"][0]
    captured = {}

    def fake_publish(payload, user, db):
        captured["package"] = payload.package
        return {"draft": {"route_variant": payload.package["route_variant"]}, "outbound": False}

    monkeypatch.setattr(automation_api, "import_route_product_draft", fake_publish)
    response = client.put(
        f"/v1/automation/route-products/{current['route_variant']}/content",
        headers={"X-CSRF-Token": csrf},
        json={
            "name": current["name"],
            "selection_title": current["selection_title"],
            "default_entry_message": current["default_entry_message"],
            "ai_guidance": current["ai_guidance"],
            "match_keywords": current["match_keywords"],
            "knowledge_facts": current["knowledge_facts"],
            "content_groups": current["content_groups"],
        },
    )
    assert response.status_code == 200
    assert captured["package"]["content_sequence"] == current["content_sequence"]
    assert captured["package"]["fixed_answers"] == current["fixed_answers"]


def test_route_asset_metadata_and_file_can_be_replaced(authenticated, session_factory, tmp_path):
    client, csrf = authenticated
    package = ROUTE_PACKAGES["peach_9d_2027"]
    asset_key = next(key for group in package["content_groups"].values() for key in group.get("asset_keys", []))
    old_file = tmp_path / "old.png"
    new_file = tmp_path / "new.png"
    old_file.write_bytes(b"old-image")
    new_file.write_bytes(b"new-image")
    with session_factory() as db:
        version = KnowledgeVersion(tenant_id=1, version_key=package["knowledge_version"], title="test", content_hash="a" * 64)
        db.add(version)
        db.flush()
        asset = MaterialAsset(
            knowledge_version_id=version.id, asset_key=asset_key, source_path=str(old_file),
            display_name="旧素材", media_type="image", usage="旧说明", available=True,
            metadata_json={"route_variants": ["peach_9d_2027"], "stored_media_id": None},
        )
        media = StoredMedia(
            tenant_id=1, original_name="new.png", media_type="image", mime_type="image/png",
            file_size=new_file.stat().st_size, storage_path=str(new_file), created_by=1,
        )
        db.add_all([asset, media])
        db.commit()
        media_id = media.id

    metadata = client.put(
        f"/v1/automation/route-products/peach_9d_2027/assets/{asset_key}",
        headers={"X-CSRF-Token": csrf},
        json={
            "display_name": "clear asset",
            "usage": "hotel introduction",
            "what_it_shows": "hotel twin room and in-room equipment",
            "feature_points": ["actual room", "oxygen equipment"],
            "customer_value": "helps the customer inspect the accommodation setup",
            "recommended_caption": "I will send the room photos first.",
            "avoid_claims": ["guarantees no altitude sickness"],
        },
    )
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["what_it_shows"] == "hotel twin room and in-room equipment"
    assert metadata.json()["feature_points"] == ["actual room", "oxygen equipment"]
    replaced = client.post(
        f"/v1/automation/route-products/peach_9d_2027/assets/{asset_key}/replace",
        headers={"X-CSRF-Token": csrf},
        json={"media_id": media_id},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["preview_url"].endswith(f"/media/{media_id}/preview")
    with session_factory() as db:
        row = db.scalar(select(MaterialAsset).where(MaterialAsset.asset_key == asset_key))
        assert row.display_name == "clear asset"
        assert row.source_path == str(new_file)
        assert row.metadata_json["stored_media_id"] == media_id
        assert row.metadata_json["customer_value"] == "helps the customer inspect the accommodation setup"
        assert row.metadata_json["avoid_claims"] == ["guarantees no altitude sickness"]


def test_route_package_import_publishes_validated_route(
    authenticated, session_factory, monkeypatch, tmp_path
):
    import app.automation_api as automation_api
    import app.automation_service as automation_service
    import app.route_packages as route_packages

    client, csrf = authenticated
    source = PACKAGE_ROOT / "peach-9d-2027" / "route-package.json"
    package = json.loads(source.read_text(encoding="utf-8"))
    package["route_variant"] = "spring_lhasa_7d_2027"
    package["branch"] = "spring_lhasa_7d"
    package["name"] = "春季拉萨7日"
    package["selection_title"] = "春季拉萨7日"
    package["sop"]["name"] = "春季拉萨7日默认SOP"
    original_root = route_packages.RUNTIME_PACKAGE_ROOT
    monkeypatch.setattr(route_packages, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
    monkeypatch.setattr(automation_api, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
    monkeypatch.setattr(automation_api, "freeze_nodes", lambda _db, nodes, _route, _tenant: nodes)
    monkeypatch.setattr(automation_service, "freeze_nodes", lambda _db, nodes, _route, _tenant: nodes)
    route_packages.reload_route_packages()
    with session_factory() as db:
        version = KnowledgeVersion(
            tenant_id=1,
            version_key=package["knowledge_version"],
            title="route import",
            content_hash="a" * 64,
        )
        db.add(version)
        db.flush()
        for asset_key in sorted({
            key
            for group in package["content_groups"].values()
            for key in group.get("asset_keys", [])
        }):
            db.add(MaterialAsset(
                knowledge_version_id=version.id,
                asset_key=asset_key,
                source_path=str(tmp_path / asset_key),
                display_name=asset_key,
                available=True,
            ))
        db.commit()
    try:
        response = client.post(
            "/v1/automation/route-products/import",
            headers={"X-CSRF-Token": csrf},
            json={"package": package},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["outbound"] is False
        assert result["draft"]["route_variant"] == "spring_lhasa_7d_2027"
        assert result["draft"]["status"] == "published"
        assert (tmp_path / "routes" / "spring_lhasa_7d_2027" / "route-package.json").is_file()
        assert "spring_lhasa_7d_2027" in route_packages.ROUTE_PACKAGES
        with session_factory() as db:
            sop = db.query(SopDefinition).filter_by(
                tenant_id=1, route_variant="spring_lhasa_7d_2027", status="running"
            ).one()
            assert sop.live_enabled is True
        config = client.get("/v1/automation/reception-config").json()
        assert config["draft_imports"] == []
    finally:
        monkeypatch.setattr(route_packages, "RUNTIME_PACKAGE_ROOT", original_root)
        monkeypatch.setattr(automation_api, "RUNTIME_PACKAGE_ROOT", original_root)
        route_packages.reload_route_packages()
