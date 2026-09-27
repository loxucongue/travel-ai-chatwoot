from copy import deepcopy
from pathlib import Path

import pytest

import app.automation_api as api
from app.route_packages import ROUTE_PACKAGES, RoutePackageError, _validate


CUSTOM = "operator_test_content"


@pytest.fixture
def content_api(authenticated, monkeypatch):
    client, csrf = authenticated
    route = "peach_9d_2027"
    current = deepcopy(ROUTE_PACKAGES[route])
    current["content_groups"][CUSTOM] = {
        "purpose": "Travel tips", "approved_text": "Travel tips.",
        "asset_keys": [], "evidence_refs": [], "initial_delivery": False,
        "delivery_mode": "text_only",
    }
    current["content_sequence"].append(CUSTOM)
    monkeypatch.setitem(ROUTE_PACKAGES, route, current)
    monkeypatch.setattr(api, "ensure_route_packages_current", lambda: None)
    published = []

    def publish(payload, user, db):
        try:
            checked = _validate(deepcopy(payload.package), Path("test-content.json"))
        except RoutePackageError as exc:
            raise api.HTTPException(422, detail={"code": "route_package_invalid", "message": str(exc)}) from exc
        published.append(checked)
        return {"draft": {"package_version": checked["package_version"]}, "outbound": False}

    monkeypatch.setattr(api, "import_route_product_draft", publish)
    payload = {
        "base_package_version": current["package_version"],
        "default_entry_message": current["default_entry_message"],
        "knowledge_facts": deepcopy(current["knowledge_facts"]),
        "content_groups": [{"key": key, **deepcopy(group)} for key, group in current["content_groups"].items()],
        "content_sequence": list(current["content_sequence"]),
    }

    def put(body):
        return client.put(f"/v1/automation/route-products/{route}/content",
                          headers={"X-CSRF-Token": csrf}, json=body)

    return current, payload, published, put


def remove_custom(payload):
    payload["content_groups"] = [g for g in payload["content_groups"] if g["key"] != CUSTOM]


def test_add_group_preserves_metadata_and_creates_unique_versions(content_api):
    current, payload, published, put = content_api
    before = deepcopy(current)
    payload["content_groups"].append({
        "key": "operator_new", "purpose": "Packing", "approved_text": "Bring warm clothes.",
        "delivery_mode": "text_only",
    })
    assert put(payload).status_code == 200
    assert put(payload).status_code == 200
    assert published[0]["package_version"] != published[1]["package_version"]
    assert published[0]["content_groups"]["operator_new"]["purpose"] == "Packing"
    assert published[0]["content_groups"]["advisor_greeting"] == current["content_groups"]["advisor_greeting"]
    assert current == before


@pytest.mark.parametrize("previous", [
    "2026-09-12.operator-235959",
    "2026-09-12.operator-" + "f" * 32,
    "2026-09-12.operator-v2-120000000000-" + "f" * 32,
    "2026-09-13.operator-v2-120000000000-" + "f" * 32,
    "2026-09-12.zz-custom",
])
def test_versions_increase_with_frozen_clock_and_legacy_versions(content_api, monkeypatch, previous):
    current, payload, published, put = content_api
    monkeypatch.setattr(api, "utcnow", lambda: "2026-09-12T12:00:00.000000+00:00")
    current["package_version"] = previous
    versions = [previous]
    for _ in range(3):
        payload["base_package_version"] = current["package_version"]
        response = put(payload)
        assert response.status_code == 200, response.text
        version = published[-1]["package_version"]
        assert version > versions[-1]
        assert ".operator-v2-" in version
        versions.append(version)
        current.update(deepcopy(published[-1]))
    assert len(set(versions)) == 4


@pytest.mark.parametrize("confirmed", [False, True])
def test_native_group_source_sop_requires_explicit_confirmation(content_api, confirmed):
    current, payload, published, put = content_api
    # Isolate the SOP dependency; fixed-answer dependencies have separate tests.
    current["fixed_answers"] = [a for a in current["fixed_answers"] if a.get("content_group_key") != "read_check"]
    before = deepcopy(current)
    payload["content_groups"] = [g for g in payload["content_groups"] if g["key"] != "read_check"]
    if confirmed:
        payload["delete_sop_group_keys"] = ["read_check"]
    response = put(payload)
    assert response.status_code == (200 if confirmed else 422), response.text
    assert current == before
    if confirmed:
        assert "read_check" not in published[-1]["content_sequence"]
        assert all(n.get("content_group_key") != "read_check" for n in published[-1]["sop"]["nodes"])
        assert published[-1]["policies"] == current["policies"]
        assert published[-1]["fixed_answers"] == current["fixed_answers"]
    else:
        assert "route_content_group_in_use" in response.text
        assert "sop.nodes" in response.text
        assert not published


@pytest.mark.parametrize("key,dependency", [
    ("hotel_reference", "policies"),
    ("peach_highlights", "fixed_answers"),
])
def test_sop_confirmation_does_not_override_policy_or_fixed_answer_guards(content_api, key, dependency):
    current, payload, published, put = content_api
    payload["content_groups"] = [g for g in payload["content_groups"] if g["key"] != key]
    payload["delete_sop_group_keys"] = [key]
    response = put(payload)
    assert response.status_code == 422
    assert "route_content_group_in_use" in response.text
    assert dependency in response.text
    assert not published


@pytest.mark.parametrize("dependency", ["alias_node", "other_node", "extension"])
def test_sop_confirmation_does_not_delete_other_references(content_api, dependency):
    current, payload, published, put = content_api
    node = next(n for n in current["sop"]["nodes"] if n.get("key") == "read_check")
    if dependency == "alias_node":
        node["key"] = "custom_followup"
    elif dependency == "other_node":
        current["sop"]["nodes"][0]["depends_on"] = "read_check"
    else:
        current["extension"] = {"group": "read_check"}
    payload["content_groups"] = [g for g in payload["content_groups"] if g["key"] != "read_check"]
    payload["delete_sop_group_keys"] = ["read_check"]
    response = put(payload)
    assert response.status_code == 422
    assert "route_content_group_in_use" in response.text
    assert not published


@pytest.mark.parametrize("keys", [["read_check"], ["missing"], [CUSTOM, CUSTOM]])
def test_sop_confirmation_must_uniquely_name_deleted_groups(content_api, keys):
    current, payload, published, put = content_api
    remove_custom(payload)
    payload["delete_sop_group_keys"] = keys
    response = put(payload)
    assert response.status_code == 422
    assert "route_content_sop_deletion_invalid" in response.text
    assert not published


@pytest.mark.parametrize("sequence", ["included", "omitted", "empty"])
def test_delete_cleans_sequence_without_mutating_current(content_api, sequence):
    current, payload, published, put = content_api
    before = deepcopy(current)
    remove_custom(payload)
    if sequence == "omitted":
        payload.pop("content_sequence")
    elif sequence == "empty":
        payload["content_sequence"] = []
    response = put(payload)
    if sequence == "empty":
        assert response.status_code == 422
        assert "route_content_sequence_empty" in response.text
        assert not published
        assert current == before
        return
    assert response.status_code == 200, response.text
    assert CUSTOM not in published[0]["content_groups"]
    assert CUSTOM not in published[0]["content_sequence"]
    assert current == before


@pytest.mark.parametrize("explicit", [False, True])
def test_fixed_answer_dependency_is_blocked_even_when_disabled(content_api, explicit):
    current, payload, published, put = content_api
    answer = deepcopy(current["fixed_answers"][0])
    answer.update(id="custom_answer", content_group_key=CUSTOM, status="disabled")
    current["fixed_answers"].append(answer)
    if explicit:
        payload["fixed_answers"] = deepcopy(current["fixed_answers"])
    remove_custom(payload)
    response = put(payload)
    assert response.status_code == 422
    assert "fixed_answers.custom_answer" in response.text
    assert not published


def test_explicitly_removed_fixed_answer_allows_group_deletion(content_api):
    current, payload, published, put = content_api
    payload["fixed_answers"] = deepcopy(current["fixed_answers"])
    current["fixed_answers"].append({**deepcopy(current["fixed_answers"][0]),
                                     "id": "custom_answer", "content_group_key": CUSTOM})
    remove_custom(payload)
    assert put(payload).status_code == 200
    assert all(a["content_group_key"] != CUSTOM for a in published[0]["fixed_answers"])


def test_delete_blocks_fixed_answer_referencing_exclusive_group_asset(content_api):
    current, payload, published, put = content_api
    current["content_groups"][CUSTOM]["asset_keys"] = ["exclusive_asset"]
    current["fixed_answers"][0]["asset_ids"].append("exclusive_asset")
    remove_custom(payload)
    response = put(payload)
    assert response.status_code == 422
    assert "route_content_group_in_use" in response.text
    assert "fixed_answers." in response.text
    assert not published


@pytest.mark.parametrize("field,value", [
    ("sop", {"nested": [{"content_group_key": CUSTOM}]}),
    ("sop", {"nodes": [{"key": "legacy", "content_group_key": CUSTOM}]}),
    ("policies", {"nested": {"groups": [CUSTOM]}}),
    ("extension", {CUSTOM: {"enabled": True}}),
    ("extension", {"fallback": CUSTOM}),
])
def test_other_package_dependencies_are_conservatively_blocked(content_api, field, value):
    current, payload, published, put = content_api
    current.setdefault(field, {}).update(value)
    remove_custom(payload)
    response = put(payload)
    assert response.status_code == 422
    assert "route_content_group_in_use" in response.text
    assert field in response.json()["error"]["dependencies"][0]
    assert not published


@pytest.mark.parametrize("customized", [False, True])
def test_only_unmodified_owned_sop_nodes_can_be_cleaned(content_api, customized):
    current, payload, published, put = content_api
    key = "operator_new"
    payload["content_groups"].append({
        "key": key, "purpose": "Packing", "approved_text": "Bring warm clothes.",
        "delivery_mode": "text_only",
    })
    payload["content_sequence"].append(key)
    assert put(payload).status_code == 200
    current.update(deepcopy(published[-1]))
    node = next(n for n in current["sop"]["nodes"] if n.get("content_group_key") == key)
    if customized:
        node["delay_minutes"] = 5
    payload["base_package_version"] = current["package_version"]
    payload["content_groups"] = [g for g in payload["content_groups"] if g["key"] != key]
    response = put(payload)
    assert response.status_code == (422 if customized else 200), response.text
    if customized:
        assert "route_content_group_in_use" in response.text
        assert len(published) == 1
    else:
        assert all(n.get("content_group_key") != key for n in published[-1]["sop"]["nodes"])


@pytest.mark.parametrize("version", [None, "stale-version"])
def test_group_changes_require_current_version(content_api, version):
    current, payload, published, put = content_api
    payload["base_package_version"] = version
    remove_custom(payload)
    response = put(payload)
    assert response.status_code == 409
    assert not published


def test_stale_edit_cannot_reintroduce_deleted_group(content_api):
    current, payload, published, put = content_api
    current["package_version"] = "new-version"
    current["content_groups"].pop(CUSTOM)
    response = put(payload)
    assert response.status_code == 409
    assert not published


@pytest.mark.parametrize("change", ["duplicate", "unknown_sequence", "invalid_key", "blank_purpose", "bad_evidence"])
def test_invalid_group_updates_do_not_publish(content_api, change):
    current, payload, published, put = content_api
    group = {"key": "operator_new", "purpose": "Packing", "approved_text": "Warm clothes."}
    if change == "duplicate":
        group = deepcopy(payload["content_groups"][0])
    elif change == "unknown_sequence":
        payload["content_sequence"].append("missing")
    elif change == "invalid_key":
        group["key"] = "bad key"
    elif change == "blank_purpose":
        group["purpose"] = " "
    elif change == "bad_evidence":
        group["evidence_refs"] = ["missing"]
    payload["content_groups"].append(group)
    response = put(payload)
    assert response.status_code == 422
    assert not published


@pytest.mark.parametrize("legacy_hex_source", [False, True])
def test_publish_add_delete_round_trip_preserves_old_sop_snapshot(
    authenticated, session_factory, monkeypatch, tmp_path, legacy_hex_source,
):
    import app.route_packages as packages
    import app.automation_service as service
    from sqlalchemy import select
    from app.models import KnowledgeVersion, MaterialAsset, SopDefinition
    from app.automation_models import SopVersion

    client, csrf = authenticated
    route = "peach_9d_2027"
    headers = {"X-CSRF-Token": csrf}
    original_root = packages.RUNTIME_PACKAGE_ROOT
    original_source_root = packages.PACKAGE_ROOT
    original_package = deepcopy(packages.ROUTE_PACKAGES[route])
    with monkeypatch.context() as isolated:
        isolated.setattr(packages, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
        isolated.setattr(api, "RUNTIME_PACKAGE_ROOT", tmp_path / "routes")
        # The test exercises real publication and snapshots, not media resolution.
        isolated.setattr(api, "freeze_nodes", lambda db, nodes, route, tenant: nodes)
        isolated.setattr(service, "freeze_nodes", lambda db, nodes, route, tenant: nodes)
        isolated.setattr(api, "utcnow", lambda: "2026-09-12T12:00:00.000000+00:00")
        if legacy_hex_source:
            import json
            source_path = tmp_path / "source" / route / "route-package.json"
            source_path.parent.mkdir(parents=True)
            original_package["package_version"] = "2026-09-12.operator-" + "f" * 32
            source_path.write_text(json.dumps(original_package, ensure_ascii=False), encoding="utf-8")
            isolated.setattr(packages, "PACKAGE_ROOT", tmp_path / "source")
        packages.reload_route_packages()
        try:
            source = deepcopy(packages.ROUTE_PACKAGES[route])
            with session_factory() as db:
                version = KnowledgeVersion(tenant_id=1, version_key=source["knowledge_version"],
                                           title="Content test", content_hash="a" * 64)
                db.add(version)
                db.flush()
                for key in {key for group in source["content_groups"].values() for key in group.get("asset_keys", [])}:
                    db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=key,
                                         source_path=str(tmp_path / key), display_name=key, available=True))
                db.commit()

            def get_product():
                response = client.get("/v1/automation/route-products")
                assert response.status_code == 200
                return next(item for item in response.json()["items"] if item["route_variant"] == route)

            product = get_product()
            product["base_package_version"] = product["package_version"]
            product["content_groups"].append({
                "key": CUSTOM, "purpose": "Packing", "approved_text": "Bring warm clothes.",
                "initial_delivery": True, "delivery_mode": "text_only",
            })
            product["content_sequence"].append(CUSTOM)
            response = client.put(f"/v1/automation/route-products/{route}/content", headers=headers, json=product)
            assert response.status_code == 200, response.text
            with session_factory() as db:
                sop = db.scalar(select(SopDefinition).where(SopDefinition.route_variant == route))
                old_version = db.scalar(select(SopVersion).where(SopVersion.sop_id == sop.id))
                old_id, old_config, old_hash = old_version.id, deepcopy(old_version.config), old_version.content_hash
                assert any(n.get("content_group_key") == CUSTOM for n in old_config["nodes"])

            added = get_product()
            assert added["package_version"] == response.json()["draft"]["package_version"]
            assert added["package_version"] > source["package_version"]
            assert next(g for g in added["content_groups"] if g["key"] == CUSTOM)["purpose"] == "Packing"
            from app.reception_v2.skill_registry import SkillRegistry
            registry = SkillRegistry()
            skill = registry.load(registry.route_skill(route))
            assert any(item['group_key'] == CUSTOM and item['text'] == 'Bring warm clothes.'
                       for item in skill['introduction'])
            assert all('content_group_candidates' not in node for node in added['default_sop']['nodes'])
            added_version = added["package_version"]
            added["base_package_version"] = added_version
            added["content_groups"] = [g for g in added["content_groups"] if g["key"] not in {CUSTOM, "read_check"}]
            rejected = client.put(f"/v1/automation/route-products/{route}/content", headers=headers, json=added)
            assert rejected.status_code == 422
            assert get_product()["package_version"] == added_version
            added["fixed_answers"] = [a for a in added["fixed_answers"] if a.get("content_group_key") != "read_check"]
            added["delete_sop_group_keys"] = ["read_check"]
            response = client.put(f"/v1/automation/route-products/{route}/content", headers=headers, json=added)
            assert response.status_code == 200, response.text
            packages.reload_route_packages()
            deleted = get_product()
            assert CUSTOM not in deleted["content_sequence"]
            assert all(g["key"] != CUSTOM for g in deleted["content_groups"])
            assert deleted["package_version"] > added_version
            assert deleted["package_version"] == response.json()["draft"]["package_version"]
            assert "read_check" not in deleted["content_sequence"]
            assert all(g["key"] != "read_check" for g in deleted["content_groups"])
            assert all(n.get("content_group_key") != "read_check" for n in packages.ROUTE_PACKAGES[route]["sop"]["nodes"])
            assert any(c["content_group_key"] == "read_check" for n in old_config["nodes"] for c in n.get("content_group_candidates", []))
            assert all(n.get("content_group_key") != CUSTOM for n in deleted["default_sop"]["nodes"])
            assert all(n.get("content_group_key") != CUSTOM for n in packages.ROUTE_PACKAGES[route]["sop"]["nodes"])
            with session_factory() as db:
                old = db.get(SopVersion, old_id)
                assert old.config == old_config
                assert old.content_hash == old_hash
                assert len(db.scalars(select(SopVersion).where(SopVersion.sop_id == old.sop_id)).all()) == 2
            stale = client.put(f"/v1/automation/route-products/{route}/content", headers=headers, json=product)
            assert stale.status_code == 409
            assert get_product()["package_version"] == deleted["package_version"]
        finally:
            isolated.setattr(packages, "RUNTIME_PACKAGE_ROOT", original_root)
            isolated.setattr(api, "RUNTIME_PACKAGE_ROOT", original_root)
            isolated.setattr(packages, "PACKAGE_ROOT", original_source_root)
            packages.reload_route_packages()
