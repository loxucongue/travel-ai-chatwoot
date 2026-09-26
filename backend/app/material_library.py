"""Local material selection and shared rehearsal deduplication. No remote writes."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from app.models import MaterialAsset, KnowledgeVersion, StoredMedia, InboxBinding, utcnow
from app.automation_models import MaterialDelivery
from app.route_reply import KNOWLEDGE_VERSION as ROUTES_1_2_VERSION
from app.asset_narratives import normalized_asset_narrative

CATALOG_VERSION = "materials-20260826-v1"
CATALOG_VERSIONS = (CATALOG_VERSION, ROUTES_1_2_VERSION)
ROUTES = {"peach_9d_2027": "2027 桃花9日", "peach_11d_2027": "2027 桃花+珠峰11日"}
ASSET_REVISIONS_KEY = "revision_archive"


def _current_revision(asset):
    metadata = deepcopy(asset.metadata_json or {})
    metadata.pop(ASSET_REVISIONS_KEY, None)
    return {"schema_version": 1, "asset_key": asset.asset_key,
            "media_id": metadata.get("stored_media_id"), "file_hash": asset.file_hash,
            "media_type": asset.media_type, "source_path": asset.source_path,
            "display_name": asset.display_name, "usage": asset.usage,
            "available": asset.available, "metadata": metadata}


def replace_asset_binding(db, asset, media, *, metadata_updates=None):
    """Archive the old binding atomically; replacement never clears a revocation."""
    version = db.get(KnowledgeVersion, asset.knowledge_version_id)
    supported = media.media_type == 'image' or (media.media_type == 'file' and media.mime_type == 'application/pdf')
    if (not version or media.tenant_id != version.tenant_id or not supported
            or not Path(media.storage_path).is_file()):
        raise ValueError("material_unavailable")
    digest = hashlib.sha256(Path(media.storage_path).read_bytes()).hexdigest()
    before = deepcopy(asset.metadata_json or {})
    metadata = deepcopy(before)
    archive = metadata.get(ASSET_REVISIONS_KEY, [])
    if not isinstance(archive, list):
        raise ValueError("material_revision_archive_invalid")
    previous = _current_revision(asset)
    changed = previous["media_id"] != media.id or previous["file_hash"] != digest
    if changed and (previous["media_id"] is not None or previous["file_hash"]):
        archive.append({**previous, "archived_at": utcnow()})
    metadata.update(metadata_updates or {})
    metadata[ASSET_REVISIONS_KEY] = archive
    metadata["stored_media_id"] = media.id
    # Existing approval controls are asset-wide kill switches, including history.
    if previous["media_id"] is not None:
        if "review_state" in before:
            metadata["review_state"] = before["review_state"]
        if "live_approved" in before:
            metadata["live_approved"] = before["live_approved"]
    if changed and media.media_type == 'file':
        metadata['live_approved'] = False
        metadata['review_state'] = 'pending'
    available = asset.available if previous["media_id"] is not None else True
    statement = update(MaterialAsset).where(
        MaterialAsset.id == asset.id, MaterialAsset.metadata_json == before,
        MaterialAsset.file_hash == asset.file_hash, MaterialAsset.available == asset.available,
    ).values(source_path=media.storage_path, file_hash=digest, media_type=media.media_type,
             available=available, metadata_json=metadata).execution_options(synchronize_session=False)
    with db.no_autoflush:
        if db.execute(statement).rowcount != 1:
            raise ValueError("material_revision_conflict")
    db.refresh(asset)


def _binding_revision(asset, media, digest, *, allow_current_hash_alias=False):
    current = _current_revision(asset)
    if current["media_id"] == media.id:
        return current
    archive = (asset.metadata_json or {}).get(ASSET_REVISIONS_KEY, [])
    if not isinstance(archive, list):
        return None
    matches = [revision for revision in archive if isinstance(revision, dict)
               and revision.get("schema_version") == 1 and revision.get("asset_key") == asset.asset_key
               and revision.get("media_id") == media.id and revision.get("file_hash") == digest]
    if matches:
        # Repeated identical bindings may have different approval histories.
        # Never choose the most permissive one to resolve an ambiguous history.
        identities = [{key: value for key, value in revision.items() if key != "archived_at"}
                      for revision in matches]
        return matches[0] if all(value == identities[0] for value in identities) else None
    if allow_current_hash_alias and current["file_hash"] == digest:
        return current
    return None


def material_live_approved(db, asset, media, *, expected_hash=None):
    """Check both current asset-wide permission and the exact revision's approval."""
    if asset is None or media is None:
        return False
    db.refresh(asset)
    version = db.get(KnowledgeVersion, asset.knowledge_version_id)
    if not version or version.tenant_id != media.tenant_id or not Path(media.storage_path).is_file():
        return False
    digest = hashlib.sha256(Path(media.storage_path).read_bytes()).hexdigest()
    revision = _binding_revision(asset, media, digest)
    current = asset.metadata_json or {}
    if expected_hash and digest != expected_hash:
        return False
    return bool(asset.available and current.get("review_state") == "evaluation_ready"
                and current.get("live_approved") is True and revision
                and revision.get("file_hash") == digest and revision.get("available")
                and revision.get("metadata", {}).get("review_state") == "evaluation_ready"
                and revision.get("metadata", {}).get("live_approved") is True)


def catalog_assets(db, tenant_id=None):
    query = select(MaterialAsset).join(KnowledgeVersion).where(
        KnowledgeVersion.version_key.in_(CATALOG_VERSIONS)
    )
    if tenant_id is not None:
        query = query.where(KnowledgeVersion.tenant_id == tenant_id)
    return db.scalars(query.order_by(MaterialAsset.id)).all()


def tenant_for_session(db, session):
    inbox = db.get(InboxBinding, session.inbox_binding_id) if session.inbox_binding_id else None
    return inbox.tenant_id if inbox else None


def by_media(db, media, *, asset_key=None):
    if not media:
        return None
    rows = [asset for asset in catalog_assets(db, media.tenant_id)
            if asset_key is None or asset.asset_key == asset_key]
    direct = next((a for a in rows if a.metadata_json.get("stored_media_id") == media.id), None)
    if direct:
        return direct
    if not Path(media.storage_path).is_file():
        return None
    digest = hashlib.sha256(Path(media.storage_path).read_bytes()).hexdigest()
    historical = [asset for asset in rows if _binding_revision(asset, media, digest) is not None]
    if len(historical) == 1:
        return historical[0]
    if historical or asset_key is not None:
        return None
    return next((a for a in rows if a.file_hash == digest), None)


def material_info(db, item, route="", tenant_id=None):
    asset = None
    media = db.get(StoredMedia, item.get("media_id")) if item.get("media_id") else None
    if media is None and not item.get("media_id") and item.get("asset_key") and tenant_id is not None:
        asset = next(
            (
                candidate
                for candidate in catalog_assets(db, tenant_id)
                if candidate.asset_key == item["asset_key"]
            ),
            None,
        )
        stored_media_id = (asset.metadata_json or {}).get("stored_media_id") if asset else None
        media = db.get(StoredMedia, stored_media_id) if stored_media_id else None
    if not media or (tenant_id is not None and media.tenant_id != tenant_id):
        raise ValueError("material_unavailable")
    path = Path(media.storage_path)
    if not path.is_file():
        raise ValueError("material_unavailable")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if item.get("media_hash") and item["media_hash"] != digest:
        raise ValueError("material_revision_changed")
    if item.get("content_type") != media.media_type:
        raise ValueError("media_type_mismatch")
    asset = asset or by_media(db, media, asset_key=item.get("asset_key"))
    if item.get("asset_key") and (not asset or asset.asset_key != item["asset_key"]):
        raise ValueError("material_binding_changed")
    family = f"file:{digest}"
    group = ""
    revision = None
    if asset:
        db.refresh(asset)
        current_meta = asset.metadata_json or {}
        if not asset.available or current_meta.get("review_state") != "evaluation_ready":
            raise ValueError("material_review_required")
        revision = _binding_revision(asset, media, digest, allow_current_hash_alias=not item.get("asset_key"))
        if revision is None:
            raise ValueError("material_binding_changed")
        meta = revision.get("metadata") or {}
        if not revision.get("available") or meta.get("review_state") != "evaluation_ready":
            raise ValueError("material_review_required")
        if digest != revision.get("file_hash"):
            raise ValueError("material_revision_changed")
        if revision.get("media_type") != media.media_type:
            raise ValueError("media_type_mismatch")
        effective_route = route or str(item.get("route_variant") or "")
        if not effective_route:
            from app.route_packages import ROUTES as ROUTE_PACKAGES

            candidates = [
                route_id
                for route_id in meta.get("route_variants", [])
                if route_id in ROUTE_PACKAGES
                and asset.asset_key in ROUTE_PACKAGES[route_id]["groups"].get(
                    "itinerary_overview", {}
                ).get("assets", [])
            ]
            if len(candidates) == 1:
                effective_route = candidates[0]
        if (effective_route not in meta.get("route_variants", [])
                or effective_route not in current_meta.get("route_variants", [])):
            raise ValueError("material_route_mismatch")
        family = meta["content_family"]
        group = f"{effective_route}:{meta.get('content_group_key') or family}"
    return {"media_id": media.id, "media_hash": digest, "asset_key": asset.asset_key if asset else None,
            "content_family": family, "content_group_key": group, "name": revision["display_name"] if revision else media.original_name,
            "content_type": media.media_type, "route_variant": effective_route if asset else route}


def freeze_nodes(db, nodes, route, tenant_id):
    from app.sop_schedule import content_items
    frozen = []
    for node in nodes:
        items, families = [], set()
        for item in content_items(node):
            if item.get("content_type", "text") != "text":
                info = material_info(db, item, route, tenant_id)
                if info["content_family"] in families:
                    raise ValueError("duplicate_material_in_group")
                families.add(info["content_family"])
                item = {**item, **info}
            items.append(item)
        frozen.append({**node, "messages": items} if node.get("messages") is not None else {**node, **items[0], "key": node["key"]})
    return frozen


def candidate_materials(db, tenant_id):
    # Missing tenant context cannot expose a different workspace's catalog.
    if tenant_id is None:
        return []
    return [{"key": a.asset_key, "name": a.display_name, "routes": a.metadata_json.get("route_variants", []),
             "media_hash": a.file_hash, "media_id": a.metadata_json.get("stored_media_id"),
             "content_type": a.media_type,
             "topic": a.usage, "family": a.metadata_json.get("content_family", f"asset:{a.asset_key}"),
             "content_group_key": a.metadata_json.get("content_group_key", ""),
             **normalized_asset_narrative(a.metadata_json)}
            for a in catalog_assets(db, tenant_id)
            if a.available and a.metadata_json.get("review_state") == "evaluation_ready"
            and Path(a.source_path).is_file()]


def resolve_materials(db, keys, route, tenant_id, *, snapshot=None):
    rows = {a.asset_key: a for a in catalog_assets(db, tenant_id)} if tenant_id is not None else {}
    result, families = [], set()
    for key in keys:
        if snapshot is not None:
            binding = (snapshot.get("asset_bindings") or {}).get(key)
            expected = (snapshot.get("asset_hashes") or {}).get(key)
            if not binding or not expected:
                raise ValueError("delivery_asset_snapshot_missing")
            info = material_info(db, {**binding, "asset_key": key, "media_hash": expected}, route, tenant_id)
            if info["media_hash"] != expected:
                raise ValueError("delivery_asset_hash_mismatch")
            if info["media_hash"] not in families:
                result.append(info)
                families.add(info["media_hash"])
            continue
        asset = rows.get(key)
        if not asset:
            raise ValueError("material_unavailable")
        info = material_info(db, {"media_id": asset.metadata_json.get("stored_media_id"), "asset_key": key,
                                 "content_type": asset.media_type}, route, tenant_id)
        if info["content_family"] not in families:
            result.append(info)
            families.add(info["content_family"])
    if len(result) > 2:
        raise ValueError("too_many_materials")
    return result


def same_content_group(asset: MaterialAsset | None, info: dict) -> bool:
    """Match equivalent reviewed content even when AI and SOP use different files."""
    if not asset or not info.get("content_group_key"):
        return False
    route, separator, group = str(info["content_group_key"]).partition(":")
    if not separator or not group:
        return False
    metadata = asset.metadata_json or {}
    return (metadata.get("content_group_key") == group
            and route in (metadata.get("route_variants") or []))


def claim_key(subject, family):
    # Re-enrollment and route changes must not reset the same photo's history.
    return hashlib.sha256(f"{subject}|{family}".encode()).hexdigest()


def previous_delivery(db, subject, info, *, include_group=False):
    # Families describe related pictures, not proof that these exact bytes were sent.
    return db.scalar(select(MaterialDelivery).where(MaterialDelivery.subject_key == subject,
        MaterialDelivery.asset_hash == info["media_hash"], MaterialDelivery.status == "simulated_delivered")
        .order_by(MaterialDelivery.confirmed_at.desc()))


def record_delivery(db, session, subject, info, business_key, source, route, *, resend=False):
    key = claim_key(subject, f"sha256:{info['media_hash']}")
    if resend:
        key = hashlib.sha256(f"{key}|explicit:{business_key}".encode()).hexdigest()
    result = db.execute(insert(MaterialDelivery).values(claim_key=key, subject_key=subject, session_id=session.id,
        business_key=business_key, content_family=info["content_family"], content_group_key=info.get("content_group_key", ""), asset_hash=info["media_hash"],
        media_id=info["media_id"], source=source, route_variant=route, status="simulated_delivered",
        confirmed_at=session.virtual_now).on_conflict_do_nothing(index_elements=[MaterialDelivery.claim_key]))
    return bool(result.rowcount)
