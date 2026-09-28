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




@pytest.mark.parametrize("key,dependency", [
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
