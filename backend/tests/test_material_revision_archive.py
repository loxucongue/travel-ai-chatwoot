from copy import deepcopy
import hashlib

import pytest

from app.live_reply import ReplyBlocked, live_materials
from app.material_library import (
    ASSET_REVISIONS_KEY, by_media, candidate_materials, material_info,
    material_live_approved, replace_asset_binding, resolve_materials,
)
from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
from app.route_packages import ROUTE_PACKAGES
from app.route_reply import bind_new_route_snapshot, route_snapshot_from_values


ROUTE = "peach_9d_2027"
KEY = "routes12-9d-itinerary"


def add_media(db, tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    media = StoredMedia(tenant_id=1, original_name=name, media_type="image", mime_type="image/png",
                        file_size=len(data), storage_path=str(path), created_by=1)
    db.add(media)
    db.flush()
    return media


@pytest.fixture
def revisions(session_factory, tmp_path):
    with session_factory() as db:
        version = KnowledgeVersion(tenant_id=1, version_key=ROUTE_PACKAGES[ROUTE]["knowledge_version"],
                                   title="Revision test", content_hash="a" * 64)
        db.add(version)
        old = add_media(db, tmp_path, "old.png", b"reviewed old image")
        new = add_media(db, tmp_path, "new.png", b"reviewed new image")
        asset = MaterialAsset(
            knowledge_version_id=version.id, asset_key=KEY, source_path=old.storage_path,
            display_name="Original reviewed image", usage="itinerary", available=True,
            file_hash=hashlib.sha256(b"reviewed old image").hexdigest(),
            metadata_json={"stored_media_id": old.id, "review_state": "evaluation_ready",
                           "live_approved": True, "route_variants": [ROUTE],
                           "content_family": "itinerary", "content_group_key": "itinerary_overview",
                           "recommended_caption": "Original reviewed caption"},
        )
        db.add(asset)
        db.commit()
        old_spec = route_snapshot_from_values(ROUTE, bind_new_route_snapshot(
            ROUTE, {}, available_materials=candidate_materials(db, 1),
        ))
        return asset.id, old.id, new.id, old_spec, deepcopy(asset.metadata_json)


def replace(client, csrf, media_id):
    response = client.post(f"/v1/automation/route-products/{ROUTE}/assets/{KEY}/replace",
                           headers={"X-CSRF-Token": csrf}, json={"media_id": media_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_replace_keeps_old_snapshot_on_old_file_and_new_selection_current(
    authenticated, session_factory, revisions,
):
    client, csrf = authenticated
    asset_id, old_id, new_id, old_spec, old_metadata = revisions
    replace(client, csrf, new_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, asset_id)
        archive = asset.metadata_json[ASSET_REVISIONS_KEY]
        assert len(archive) == 1
        assert archive[0]["metadata"] == old_metadata
        assert archive[0]["media_id"] == old_id
        assert archive[0]["file_hash"] == old_spec["asset_hashes"][KEY]
        assert archive[0]["available"] is True
        assert ASSET_REVISIONS_KEY not in archive[0]["metadata"]
        current = candidate_materials(db, 1)
        assert len(current) == 1 and current[0]["media_id"] == new_id
        new_spec = route_snapshot_from_values(ROUTE, bind_new_route_snapshot(ROUTE, {}, available_materials=current))
        for snapshot, media_id in [(old_spec, old_id), (new_spec, new_id)]:
            info = resolve_materials(db, [KEY], ROUTE, 1, snapshot=snapshot)[0]
            assert info["media_id"] == media_id
            assert info["media_hash"] == snapshot["asset_hashes"][KEY]
            assert live_materials(db, [KEY], ROUTE, 1, snapshot=snapshot)[0] == info
            assert by_media(db, db.get(StoredMedia, media_id)).id == asset_id


@pytest.mark.parametrize("revocation", ["live_approved", "review_state", "available"])
def test_explicit_revocation_blocks_both_revisions_even_after_another_replace(
    authenticated, session_factory, revisions, revocation,
):
    client, csrf = authenticated
    asset_id, _, new_id, old_spec, _ = revisions
    replace(client, csrf, new_id)
    with session_factory() as db:
        new_spec = route_snapshot_from_values(ROUTE, bind_new_route_snapshot(
            ROUTE, {}, available_materials=candidate_materials(db, 1),
        ))
        asset = db.get(MaterialAsset, asset_id)
        if revocation == "available":
            asset.available = False
        else:
            asset.metadata_json = {**asset.metadata_json, revocation: False if revocation == "live_approved" else "revoked"}
        db.commit()
    replace(client, csrf, new_id)
    with session_factory() as db:
        for spec in (old_spec, new_spec):
            with pytest.raises((ValueError, ReplyBlocked)):
                live_materials(db, [KEY], ROUTE, 1, snapshot=spec)
            if revocation != "live_approved":
                with pytest.raises(ValueError, match="material_review_required"):
                    resolve_materials(db, [KEY], ROUTE, 1, snapshot=spec)


def test_unarchived_old_binding_and_missing_media_never_fall_back(
    authenticated, session_factory, revisions,
):
    client, csrf = authenticated
    asset_id, old_id, new_id, old_spec, _ = revisions
    replace(client, csrf, new_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, asset_id)
        asset.metadata_json = {key: value for key, value in asset.metadata_json.items() if key != ASSET_REVISIONS_KEY}
        db.commit()
        assert by_media(db, db.get(StoredMedia, old_id), asset_key=KEY) is None
        with pytest.raises(ValueError, match="material_binding_changed"):
            resolve_materials(db, [KEY], ROUTE, 1, snapshot=old_spec)
        with pytest.raises(ValueError, match="material_unavailable"):
            material_info(db, {"media_id": 999999, "asset_key": KEY, "content_type": "image"}, ROUTE, 1)


def test_old_unapproved_revision_does_not_borrow_new_revision_live_approval(
    authenticated, session_factory, revisions,
):
    client, csrf = authenticated
    asset_id, old_id, new_id, old_spec, _ = revisions
    with session_factory() as db:
        asset = db.get(MaterialAsset, asset_id)
        asset.metadata_json = {**asset.metadata_json, "live_approved": False}
        db.commit()
    replace(client, csrf, new_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, asset_id)
        asset.metadata_json = {**asset.metadata_json, "live_approved": True}
        db.commit()
        assert not material_live_approved(db, asset, db.get(StoredMedia, old_id))
        assert material_live_approved(db, asset, db.get(StoredMedia, new_id))
        with pytest.raises(ReplyBlocked, match="material_not_approved_for_live"):
            live_materials(db, [KEY], ROUTE, 1, snapshot=old_spec)


def test_repeated_endpoint_replacements_append_without_nested_archives(
    authenticated, session_factory, revisions, tmp_path,
):
    client, csrf = authenticated
    asset_id, old_id, new_id, old_spec, _ = revisions
    replace(client, csrf, new_id)
    replace(client, csrf, new_id)
    with session_factory() as db:
        third = add_media(db, tmp_path, "third.png", b"third image")
        db.commit()
        third_id = third.id
    replace(client, csrf, third_id)
    with session_factory() as db:
        archive = db.get(MaterialAsset, asset_id).metadata_json[ASSET_REVISIONS_KEY]
        assert [entry["media_id"] for entry in archive] == [old_id, new_id]
        assert all(ASSET_REVISIONS_KEY not in entry["metadata"] for entry in archive)
        assert resolve_materials(db, [KEY], ROUTE, 1, snapshot=old_spec)[0]["media_id"] == old_id


def test_stale_revision_writer_cannot_erase_archive_or_revocation(session_factory, revisions):
    asset_id, _, new_id, _, _ = revisions
    with session_factory() as first, session_factory() as second:
        stale = first.get(MaterialAsset, asset_id)
        other = second.get(MaterialAsset, asset_id)
        other.metadata_json = {**other.metadata_json, "live_approved": False}
        second.commit()
        with pytest.raises(ValueError, match="material_revision_conflict"):
            replace_asset_binding(first, stale, first.get(StoredMedia, new_id))
        first.rollback()
        assert first.get(MaterialAsset, asset_id).metadata_json["live_approved"] is False


def test_archived_bytes_and_tenant_still_checked(authenticated, session_factory, revisions, tmp_path):
    client, csrf = authenticated
    _, _, new_id, old_spec, _ = revisions
    replace(client, csrf, new_id)
    with session_factory() as db:
        with pytest.raises(ValueError, match="material_unavailable"):
            resolve_materials(db, [KEY], ROUTE, 2, snapshot=old_spec)
        (tmp_path / "old.png").write_bytes(b"tampered")
        with pytest.raises(ValueError, match="material_revision_changed"):
            resolve_materials(db, [KEY], ROUTE, 1, snapshot=old_spec)


def test_alternate_bind_endpoint_also_archives_old_revision(authenticated, session_factory, revisions):
    client, csrf = authenticated
    asset_id, old_id, new_id, old_spec, _ = revisions
    response = client.post(f"/v1/knowledge/assets/{asset_id}/bind",
                           headers={"X-CSRF-Token": csrf}, json={"media_id": new_id})
    assert response.status_code == 200, response.text
    with session_factory() as db:
        assert db.get(MaterialAsset, asset_id).metadata_json[ASSET_REVISIONS_KEY][0]["media_id"] == old_id
        assert resolve_materials(db, [KEY], ROUTE, 1, snapshot=old_spec)[0]["media_id"] == old_id


def test_persisted_live_plan_sends_archived_file_after_real_replace(
    authenticated, session_factory, monkeypatch, tmp_path,
):
    from test_live_reply import setup
    from test_delivery_failure_pause import _media_plan
    from app import live_reply as live
    from app.models import OutboundMessage
    from sqlalchemy import select

    client, csrf = authenticated
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        job, state, run, asset_id = _media_plan(db, tmp_path)
        asset = db.get(MaterialAsset, asset_id)
        # The helper's material fixture uses the shared catalog; this endpoint
        # operates on the route package's knowledge version.
        version = db.get(KnowledgeVersion, asset.knowledge_version_id)
        version.version_key = ROUTE_PACKAGES[ROUTE]["knowledge_version"]
        old_media_id = asset.metadata_json["stored_media_id"]
        media = add_media(db, tmp_path, "replacement.png", b"new live picture")
        db.commit()
        media_id = media.id
        ids = job.id, state.id, run.id
    replace(client, csrf, media_id)
    with session_factory() as db:
        from app.live_reply_models import LiveReplyJob
        from app.models import AiRun, ConversationState
        live._execute_persisted_reply(db, fake, db.get(LiveReplyJob, ids[0]),
                                      db.get(ConversationState, ids[1]), db.get(AiRun, ids[2]))
        db.commit()
        outbound = db.scalar(select(OutboundMessage).where(OutboundMessage.media_id.is_not(None)))
        assert outbound.media_id == old_media_id
        assert outbound.status in {"submitted", "sent", "delivered", "read"}
    assert len(fake.sent) == 1
