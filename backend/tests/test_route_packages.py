import hashlib


import json


from copy import deepcopy


from pathlib import Path


import pytest


from sqlalchemy import select


from app.config import settings


from app.models import KnowledgeVersion, MaterialAsset, SopDefinition, StoredMedia


from app.route_packages import (
    PACKAGE_ROOT, ROUTE_PACKAGES, RoutePackageError, _validate,
)











def test_first_wave_order_follows_business_documents():
    nine = ROUTE_PACKAGES["peach_9d_2027"]
    eleven = ROUTE_PACKAGES["peach_11d_2027"]
    assert nine["content_sequence"][:6] == [
        "advisor_greeting", "itinerary_overview", "peach_highlights",
        "hotel_reference", "vehicle_reference", "vehicle_oxygen",
    ]
    assert eleven["content_sequence"][:4] == [
        "advisor_greeting", "itinerary_overview", "peach_highlights", "hotel_reference",
    ]
    assert eleven["content_sequence"].index("hotel_reference") < eleven["content_sequence"].index("rongbuk_upgrade")
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


def test_website_scripts_are_usable_without_pending_review():
    for package in ROUTE_PACKAGES.values():
        answers = {answer["id"]: answer for answer in package["fixed_answers"]}
        assert answers["hotel"]["status"] == "active"
        assert "85-90%" in answers["hotel"]["answer_text"]
        assert answers["vehicle"]["status"] == "active"
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
            assert answers[answer_id]["status"] == "active"


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










def test_route_reference_copy_is_conversational_not_generic_read_check():
    for package in ROUTE_PACKAGES.values():
        groups = package["content_groups"]
        read_check = groups["read_check"]["approved_text"]
        assert "有看到了嗎" not in read_check
        assert "還滿意嗎" not in read_check
        assert "頁面展示" not in groups["hotel_reference"]["approved_text"]
        assert "頁面提供" not in groups["vehicle_reference"]["approved_text"]
        assert "LINE QR Code" in groups["contact_request"]["approved_text"]
        assert "微信或" not in groups["contact_request"]["approved_text"]
        assert "不確定時不再追問" not in groups["contact_request"]["approved_text"]
        assert "邀請客戶" not in groups["contact_request"]["approved_text"]


def test_hotel_copy_matches_the_room_and_oxygen_images_sent_as_one_group():
    for package in ROUTE_PACKAGES.values():
        hotel = package["content_groups"]["hotel_reference"]
        assert hotel["asset_keys"] == ["routes12-hilton-room", "routes12-hilton-oxygen"]
        assert "希爾頓" in hotel["approved_text"]
        assert "85-90%" in hotel["approved_text"]
        assert "客戶詢問" not in hotel["approved_text"]






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
        assert "accommodation_summary" in item["content_sequence"]
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
        assert 'journey_policy' not in item and 'default_sop' not in item
        assert item["readiness"]["assets_total"] == len(item["assets"])
        for asset in item["assets"]:
            assert set(asset) >= {
                "what_it_shows", "feature_points", "customer_value",
                "recommended_caption", "avoid_claims",
            }
            assert isinstance(asset["feature_points"], list)
            assert isinstance(asset["avoid_claims"], list)


def test_reception_configuration_rejects_more_than_twenty_silence_nodes(authenticated):
    client, csrf = authenticated
    response = client.patch("/v1/automation/reception-config", headers={"X-CSRF-Token":csrf},
                            json={"silence":{"intervals_minutes":list(range(1,22))}})
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
    import app.route_packages as route_packages

    client, csrf = authenticated
    source = PACKAGE_ROOT / "peach-9d-2027" / "route-package.json"
    package = json.loads(source.read_text(encoding="utf-8"))
    package["route_variant"] = "spring_lhasa_7d_2027"
    package["branch"] = "spring_lhasa_7d"
    package["name"] = "春季拉萨7日"
    package["selection_title"] = "春季拉萨7日"
    original_root = route_packages.RUNTIME_PACKAGE_ROOT
    monkeypatch.setattr(route_packages, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
    monkeypatch.setattr(automation_api, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
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
        config = client.get("/v1/automation/reception-config").json()
        assert config["draft_imports"] == []
    finally:
        monkeypatch.setattr(route_packages, "RUNTIME_PACKAGE_ROOT", original_root)
        monkeypatch.setattr(automation_api, "RUNTIME_PACKAGE_ROOT", original_root)
        route_packages.reload_route_packages()
