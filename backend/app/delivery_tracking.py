"""Receipt-derived live progress, without a new table or transaction commits.

Integration (not installed by this module): live_reply/live_sop must attach before
their pre-send commit, preserve content_attributes, and record after setting the
channel status. delivery_status.apply_receipt must record after reconciliation,
including failed and unchanged receipts. Do not retain optimistic group markers.
This validates evidence, not the bytes ultimately sent by an unintegrated sender.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.models import ConversationJourney, ConversationState, OutboundMessage, StoredMedia, utcnow
from app.route_reply import (
    CONTENT_PROGRESS_KEY, ROUTE_SNAPSHOTS_KEY, group_requirements_from_values,
    route_snapshot_from_values,
)

DELIVERY_ITEM_KEY = "_delivery_item"
ASSET_BINDINGS_KEY = "_delivery_asset_bindings"
CONFIRMED_STATUSES = frozenset({"sent", "delivered", "read"})


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pin_route_assets(db, journey, tenant_id):
    """Atomically pin every route asset before the first send; return key/hash map.

    Call immediately after route binding. Repeated calls return existing pins
    without consulting the mutable catalog. Complete verified snapshot hashes
    are already approved references, not a historical catalog rebootstrap; actual
    media bytes are checked by attach_delivery_item before send. Does not commit.
    """
    from app.material_library import catalog_assets

    state = db.get(ConversationState, journey.conversation_state_id)
    if state is None or state.tenant_id != tenant_id:
        raise ValueError("delivery_journey_mismatch")
    spec = route_snapshot_from_values(journey.route_variant, journey.slots)
    if spec is None:
        raise ValueError("delivery_snapshot_unknown")
    required = {key for group in spec["groups"].values() for key in group.get("assets", [])}
    slots = deepcopy(journey.slots or {})
    route = journey.route_variant
    bindings = (slots.get(ASSET_BINDINGS_KEY) or {}).get(route)
    snapshot_hashes = spec.get("asset_hashes")
    if isinstance(snapshot_hashes, dict) and required <= snapshot_hashes.keys():
        pinned = {key: snapshot_hashes[key] for key in sorted(required)}
        snapshot_bindings = spec.get("asset_bindings") or {}
        for key, digest in pinned.items():
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError("delivery_asset_hash_invalid")
            reference = snapshot_bindings.get(key)
            if reference is not None and (
                    not isinstance(reference, dict) or reference.get("asset_key") != key
                    or reference.get("media_hash") != digest):
                raise ValueError("delivery_asset_hash_mismatch")
            if isinstance(bindings, dict) and key in bindings and bindings[key] != digest:
                raise ValueError("delivery_asset_hash_mismatch")
        slots.setdefault(ASSET_BINDINGS_KEY, {})[route] = pinned
        journey.slots = slots
        return deepcopy(pinned)
    if isinstance(bindings, dict) and required <= bindings.keys():
        return {key: bindings[key] for key in sorted(required)}
    prior = db.scalars(select(OutboundMessage).where(
        OutboundMessage.conversation_state_id == journey.conversation_state_id,
    )).all()
    if (bindings or journey.sent_groups or (slots.get(CONTENT_PROGRESS_KEY) or {}).get(route)
            or any(not (row.content_attributes or {}).get(DELIVERY_ITEM_KEY)
                   or row.content_attributes[DELIVERY_ITEM_KEY].get("route") == route for row in prior)):
        raise ValueError("delivery_asset_history_unknown")
    catalog = catalog_assets(db, tenant_id)
    pinned = {}
    for key in sorted(required):
        candidates = [asset for asset in catalog if asset.asset_key == key and asset.available
                      and (asset.metadata_json or {}).get("live_approved") is True
                      and (asset.metadata_json or {}).get("review_state") == "evaluation_ready"
                      and route in (asset.metadata_json or {}).get("route_variants", [])]
        if not candidates or len({asset.file_hash for asset in candidates}) != 1:
            raise ValueError("delivery_asset_not_approved")
        asset = candidates[-1]
        media_id = (asset.metadata_json or {}).get("stored_media_id")
        media = db.get(StoredMedia, media_id) if media_id else None
        if media is None or media.tenant_id != tenant_id:
            raise ValueError("delivery_media_unavailable")
        try:
            with Path(media.storage_path).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise ValueError("delivery_media_unavailable") from exc
        expected = (spec.get("asset_hashes") or {}).get(key) or asset.file_hash
        if not expected or digest != expected or digest != asset.file_hash:
            raise ValueError("delivery_asset_hash_mismatch")
        pinned[key] = digest
    slots.setdefault(ASSET_BINDINGS_KEY, {})[route] = pinned
    journey.slots = slots
    return deepcopy(pinned)


def _asset_hash(db, out, journey, spec, asset_key):
    media = db.get(StoredMedia, out.media_id) if out.media_id else None
    state = db.get(ConversationState, out.conversation_state_id)
    if not media or not state or media.tenant_id != state.tenant_id:
        raise ValueError("delivery_media_unavailable")
    try:
        with Path(media.storage_path).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ValueError("delivery_media_unavailable") from exc
    route = journey.route_variant
    slots = deepcopy(journey.slots or {})
    bindings = slots.setdefault(ASSET_BINDINGS_KEY, {}).setdefault(route, {})
    expected = (spec.get("asset_hashes") or {}).get(asset_key) or bindings.get(asset_key)
    if not expected:
        # Old history cannot be retroactively certified against today's catalog.
        previous = db.scalars(select(OutboundMessage).where(
            OutboundMessage.conversation_state_id == out.conversation_state_id,
        )).all()
        history = (slots.get(CONTENT_PROGRESS_KEY) or {}).get(route, {})
        if (any(not isinstance(value, dict) or value.get("schema_version") != 2
                or value.get("history_unknown") for value in history.values())
                or any(group not in history for group in journey.sent_groups or [])
                or any(row is not out and not (row.content_attributes or {}).get(DELIVERY_ITEM_KEY)
                       for row in previous)):
            raise ValueError("delivery_asset_history_unknown")
        from app.material_library import catalog_assets
        approved = [asset for asset in catalog_assets(db, state.tenant_id)
                    if asset.asset_key == asset_key and asset.available
                    and (asset.metadata_json or {}).get("live_approved") is True
                    and (asset.metadata_json or {}).get("review_state") == "evaluation_ready"
                    and route in (asset.metadata_json or {}).get("route_variants", [])]
        hashes = {asset.file_hash for asset in approved}
        if len(hashes) != 1 or digest not in hashes:
            raise ValueError("delivery_asset_not_approved")
        expected = digest
        bindings[asset_key] = digest
    if digest != expected:
        raise ValueError("delivery_asset_hash_mismatch")
    return digest, slots


def attach_delivery_item(db, out, journey, group_keys, asset_key="", plan_version="", item_id=""):
    """Attach once before send; identical reattachment is a no-op, rebinding fails.

    The snapshot must already be bound by prepare_route_reply. Asset hashes may
    be supplied in snapshot spec.asset_hashes; absent hashes are pinned separately
    from the current approved catalog only for new, verifiable history.
    """
    if out.conversation_state_id != journey.conversation_state_id:
        raise ValueError("delivery_journey_mismatch")
    if isinstance(group_keys, str):
        raise ValueError("delivery_group_keys_must_be_list")
    groups = sorted(set(group_keys or []))
    if any(not isinstance(key, str) or not key for key in groups):
        raise ValueError("delivery_invalid_group_key")
    attrs = deepcopy(out.content_attributes or {})
    old = attrs.get(DELIVERY_ITEM_KEY)
    route = journey.route_variant or ""
    version = str(plan_version or journey.knowledge_version_key or "")
    identity = {"group_keys": groups, "route": route, "plan_version": version,
                "asset_key": asset_key, "item_id": item_id or (old or {}).get("item_id")
                or out.idempotency_key or str(uuid4()),
                "content_hash": _hash(out.content or ""), "media_id": out.media_id,
                "content_type": out.content_type or "text"}
    if old is not None:
        if not isinstance(old, dict) or any(old.get(key) != value for key, value in identity.items()):
            raise ValueError("delivery_item_immutable")
        return deepcopy(old)
    spec = route_snapshot_from_values(route, journey.slots)
    snapshot = ((journey.slots or {}).get(ROUTE_SNAPSHOTS_KEY) or {}).get(route, {})
    metadata = {**identity, "schema_version": 1, "snapshot_digest": snapshot.get("digest", ""),
                "text_group_keys": [], "asset_group_keys": [], "asset_hash": "",
                "history_unknown": spec is None}
    from app.reception_v2.events import answer_receipt
    if out.source_type == 'ai' and out.source_id:
        from app.live_reply_models import LiveReplyJob
        source = db.get(LiveReplyJob, out.source_id)
        if source and source.engine_version == 'v2' and source.conversation_state_id == out.conversation_state_id:
            metadata['v2_answer'] = answer_receipt(source.decision or {}, out.content or '')
    elif out.source_type == 'sop' and out.source_id:
        from app.automation_models import LiveSopEnrollment, LiveSopJob
        enrollment = db.get(LiveSopEnrollment, out.source_id)
        if enrollment and enrollment.engine_version == 'v2' and enrollment.conversation_state_id == out.conversation_state_id:
            matches = [job for job in db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment.id)).all()
                       if out.idempotency_key.startswith(f'live-sop:{enrollment.id}:{job.node_key}:')]
            if matches:
                source = max(matches, key=lambda job: len(job.node_key))
                metadata['v2_answer'] = answer_receipt((source.payload or {}).get('model_decision') or {}, out.content or '')
    if spec is not None:
        if any(group not in spec["groups"] for group in groups):
            raise ValueError("delivery_group_not_in_snapshot")
        if asset_key:
            matching = [key for key in groups if asset_key in spec["groups"][key].get("assets", [])]
            if not matching:
                raise ValueError("delivery_asset_not_in_group")
            digest, slots = _asset_hash(db, out, journey, spec, asset_key)
            journey.slots = slots
            metadata.update(asset_hash=digest, asset_group_keys=matching)
        elif not out.media_id and (out.content_type or "text") in {"text", "input_select"}:
            metadata["text_group_keys"] = [key for key in groups
                if spec["groups"][key].get("text") and out.content == spec["groups"][key]["text"]]
    attrs[DELIVERY_ITEM_KEY] = metadata
    attrs["delivery_item"] = {key: deepcopy(metadata[key]) for key in
                              ("group_keys", "route", "plan_version", "item_id", "asset_key")}
    out.content_attributes = attrs
    return deepcopy(metadata)


def find_existing_delivery(db, out, journey):
    """Find an approved duplicate after attach and BEFORE adding/committing out.

    Return the original row (including submitted), or None. Unknown/failed matches
    block even if another matching row succeeded. No rows are added or flushed.
    Item IDs and plan versions identify attempts, not approval; snapshot digest,
    group set, reviewed text or pinned asset key/hash identify approved content.
    Callers must serialize check-and-insert per conversation to prevent races.
    """
    if out.conversation_state_id != journey.conversation_state_id:
        raise ValueError("delivery_journey_mismatch")
    route = journey.route_variant
    spec = route_snapshot_from_values(route, journey.slots)
    if spec is None:
        return None
    snapshot = ((journey.slots or {}).get(ROUTE_SNAPSHOTS_KEY) or {}).get(route, {})

    def reference(row):
        item = (row.content_attributes or {}).get(DELIVERY_ITEM_KEY)
        if (not isinstance(item, dict) or item.get("schema_version") != 1
                or item.get("history_unknown") or item.get("route") != route
                or item.get("snapshot_digest") != snapshot.get("digest")
                or item.get("content_hash") != _hash(row.content or "")
                or item.get("media_id") != row.media_id
                or item.get("content_type") != (row.content_type or "text")):
            return None
        groups = item.get("group_keys")
        if not isinstance(groups, list) or not groups or any(
                not isinstance(key, str) or key not in spec["groups"] for key in groups):
            return None
        groups = tuple(sorted(set(groups)))
        asset = item.get("asset_key")
        if asset:
            expected = ((spec.get("asset_hashes") or {}).get(asset)
                        or (((journey.slots or {}).get(ASSET_BINDINGS_KEY) or {}).get(route) or {}).get(asset))
            if (not row.media_id or not expected or item.get("asset_hash") != expected
                    or any(asset not in spec["groups"][key].get("assets", []) for key in groups)
                    or set(item.get("asset_group_keys") or []) != set(groups)):
                return None
            return ("asset", groups, asset, expected)
        if (row.media_id or (row.content_type or "text") not in {"text", "input_select"}
                or set(item.get("text_group_keys") or []) != set(groups)
                or any(not spec["groups"][key].get("text")
                       or row.content != spec["groups"][key]["text"] for key in groups)):
            return None
        # Interactive options are part of the sent text item's behavior.
        return ("text", groups, row.content, row.content_type or "text",
                (row.content_attributes or {}).get("items"))

    identity = reference(out)
    if identity is None:
        return None
    with db.no_autoflush:
        rows = db.scalars(select(OutboundMessage).where(
            OutboundMessage.conversation_state_id == journey.conversation_state_id,
            OutboundMessage.status.in_([
                "submitted", "sent", "delivered", "read", "submission_unknown", "unknown", "failed",
            ]),
        ).order_by(OutboundMessage.id)).all()
    matches = [row for row in rows if row is not out and reference(row) == identity]
    if any(row.status in {"submission_unknown", "unknown"} for row in matches):
        raise ValueError("submission_unknown_reconcile_required")
    if any(row.status == "failed" for row in matches):
        raise ValueError("channel_send_failed")
    rank = {"submitted": 0, "sent": 1, "delivered": 2, "read": 3}
    return max(matches, key=lambda row: rank[row.status]) if matches else None


def refresh_delivery_progress(db, journey):
    """Recompute from persisted evidence; never send, retry, or commit.

    Unknown submissions pause automatic continuation via history_unknown until a
    definite receipt arrives. Legacy completion is not accepted as evidence.
    Serialise per conversation in the caller, as with other journey mutations.
    """
    db.flush()
    rows = db.scalars(select(OutboundMessage).where(
        OutboundMessage.conversation_state_id == journey.conversation_state_id,
    ).order_by(OutboundMessage.id)).all()
    slots = deepcopy(journey.slots or {})
    previous = slots.get(CONTENT_PROGRESS_KEY) or {}
    progress = {}

    def entry(route, group):
        return progress.setdefault(route, {}).setdefault(group, {
            "schema_version": 2, "text_delivered": False, "asset_keys": [],
            "topic_covered": False, "history_unknown": False,
        })

    tracked = set()
    v2_receipts = []
    for out in rows:
        item = (out.content_attributes or {}).get(DELIVERY_ITEM_KEY)
        if not isinstance(item, dict) or item.get("schema_version") != 1:
            continue
        route = item.get("route", "")
        snapshot = ((slots.get(ROUTE_SNAPSHOTS_KEY) or {}).get(route) or {})
        valid = (not item.get("history_unknown")
                 and route_snapshot_from_values(route, slots) is not None
                 and item.get("snapshot_digest") == snapshot.get("digest")
                 and item.get("content_hash") == _hash(out.content or "")
                 and item.get("media_id") == out.media_id
                 and item.get("content_type") == out.content_type)
        if valid and out.status in CONFIRMED_STATUSES and isinstance(item.get('v2_answer'), dict):
            v2_receipts.append(item['v2_answer'])
        for group in item.get("group_keys", []):
            tracked.add((route, group))
            value = entry(route, group)
            if not valid or out.status in {"submission_unknown", "unknown"}:
                value["history_unknown"] = True
            if not valid or out.status not in CONFIRMED_STATUSES:
                continue
            value["topic_covered"] = True
            value["text_delivered"] |= group in item.get("text_group_keys", [])
            if group in item.get("asset_group_keys", []) and item.get("asset_hash"):
                value["asset_keys"] = sorted(set(value["asset_keys"]) | {item["asset_key"]})
    # Preserve topic coverage separately, but never bootstrap missing receipts.
    for route, values in previous.items():
        if not isinstance(values, dict):
            continue
        for group, old in values.items():
            if not isinstance(old, dict):
                continue
            value = entry(route, group)
            if (route, group) not in tracked:
                value["topic_covered"] = bool(old.get("topic_covered"))
                value["history_unknown"] = bool(old.get("history_unknown") or old.get("text_delivered")
                                                 or old.get("asset_keys") or old.get("schema_version") != 2)
    for group in journey.sent_groups or []:
        if (journey.route_variant, group) not in tracked:
            entry(journey.route_variant, group)["history_unknown"] = True
    slots[CONTENT_PROGRESS_KEY] = progress
    if '_v2_state' in slots or v2_receipts:
        from app.reception_v2.events import rebuild_answers
        slots = rebuild_answers(slots, v2_receipts)
    completed = []
    for group, value in progress.get(journey.route_variant, {}).items():
        requirements = group_requirements_from_values(journey.route_variant, slots, group)
        if (requirements is not None and not value["history_unknown"]
                and (not requirements[0] or value["text_delivered"])
                and requirements[1] <= set(value["asset_keys"])):
            completed.append(group)
    if slots != journey.slots or completed != journey.sent_groups:
        journey.slots = slots
        journey.sent_groups = completed
        journey.version = (journey.version or 0) + 1
        journey.updated_at = utcnow()
    return deepcopy(progress.get(journey.route_variant, {}))


def record_delivery_progress(db, out):
    """Receipt hook: call after assigning reconciled out.status, even on failure."""
    from app.live_reply_models import LiveReplyJob

    attrs = deepcopy(out.content_attributes or {})
    item = attrs.get(DELIVERY_ITEM_KEY)
    if isinstance(attrs.get("delivery_item"), dict):
        attrs["delivery_item"]["status"] = out.status
        out.content_attributes = attrs
    jobs = db.scalars(select(LiveReplyJob).where(
        LiveReplyJob.conversation_state_id == out.conversation_state_id,
    )).all()
    for job in jobs:
        trace = deepcopy(job.trace or {})
        plan = trace.get("delivery_plan")
        if not isinstance(plan, list):
            continue
        changed = False
        for part in plan:
            if not isinstance(part, dict):
                continue
            by_outbound = out.id is not None and part.get("outbound_id") == out.id
            by_item = (out.source_type == "ai" and out.source_id == job.id
                       and isinstance(item, dict) and bool(item.get("item_id"))
                       and part.get("item_id") == item["item_id"]
                       and part.get("outbound_id") in (None, out.id))
            if by_outbound or by_item:
                if part.get("status") != out.status or part.get("outbound_id") != out.id:
                    part.update(status=out.status, outbound_id=out.id)
                    changed = True
        if changed:
            job.trace = trace
    if not (out.content_attributes or {}).get(DELIVERY_ITEM_KEY):
        return None
    journey = db.scalar(select(ConversationJourney).where(
        ConversationJourney.conversation_state_id == out.conversation_state_id,
    ))
    return refresh_delivery_progress(db, journey) if journey is not None else None
