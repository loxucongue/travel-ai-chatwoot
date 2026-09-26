"""Pure delivery contract shared by playground and live execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math

from app.route_packages import ROUTES


DELIVERY_PLAN_VERSION = "delivery-plan-v2"


@dataclass(frozen=True)
class DeliveryPart:
    kind: str
    content: str = ""
    material: dict | None = None
    part_id: str = ""
    plan_version: str = DELIVERY_PLAN_VERSION
    content_group_key: str = ""
    interval_seconds: float = 0
    is_follow_up: bool = False
    follow_up_type: str = ""
    follow_up_field: str = ""


def expand_static_delivery_nodes(nodes: list[dict]) -> list[dict]:
    """Split static groups into interruptible jobs without changing input data.

    Call only for model_route/passive_route enrollment, not manual SOPs.
    The scheduler links adjacent previous_node entries, including the node
    following an expanded group. Reapplying this helper is a no-op by value.
    """
    result = []
    reserved_keys = {str(node["key"]) for node in nodes}
    for node in nodes:
        messages = node.get("messages")
        if node.get("journey_trigger") or not messages or len(messages) <= 1:
            result.append(deepcopy(node))
            continue
        raw_interval = node.get("delivery_interval_seconds", 2)
        raw_interval = 2 if raw_interval is None else raw_interval
        try:
            interval = int(raw_interval)
            if interval < 0 or interval != float(raw_interval):
                raise ValueError
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("delivery_interval_invalid") from exc
        original_key = str(node["key"])
        for index, message in enumerate(messages):
            part = deepcopy(node)
            part["messages"] = [deepcopy(message)]
            part.pop("delivery_interval_seconds", None)
            if index < len(messages) - 1:
                part.pop("deferred_follow_up", None)
            if index:
                salt = 0
                while True:
                    digest = sha256(json.dumps(
                        [original_key, index, salt], separators=(",", ":"),
                    ).encode()).hexdigest()[:24]
                    key = f"{original_key[:40]}:part:{digest}"
                    if key not in reserved_keys:
                        break
                    salt += 1
                reserved_keys.add(key)
                part.update(key=key, schedule_type="relative", basis="previous_node",
                            delay_minutes=0, delay_seconds=interval)
                for field in ("fixed_at", "day_number", "time_of_day"):
                    part.pop(field, None)
            result.append(part)
    return result


def delivery_mode_for(
    route_variant: str,
    content_group_keys: list[str] | None,
    asset_keys: list[str] | set[str] | None,
    *,
    route_spec: dict | None = None,
) -> str:
    """Resolve order from a bound snapshot, or current routes when omitted."""
    route = route_spec if route_spec is not None else ROUTES.get(str(route_variant or ""))
    selected = set(asset_keys or [])
    if not route or not selected:
        return "text_then_assets"
    for key in content_group_keys or []:
        group = route.get("groups", {}).get(key, {})
        if selected.intersection(group.get("assets") or []):
            return str(group.get("delivery_mode") or "assets_then_text")
    return "text_then_assets"


def ordered_delivery_parts(
    body: str,
    materials: list[dict],
    delivery_mode: str,
    *,
    plan_id: str = "",
    content_group_key: str = "",
    interval_seconds: float = 0,
    follow_up_question: str = "",
    follow_up_type: str = "",
    follow_up_field: str = "",
    follow_up_group_key: str = "",
    text_segments: list[str] | None = None,
) -> list[DeliveryPart]:
    """Order body/assets, then the independent final question.

    IDs are deterministic for a plan scope and its ordered delivery envelope.
    Persist the result before sending and reuse it on retries. Intervals mean
    delay before this part; the first part never waits. Material dictionaries
    are deep-copied snapshots, isolated from caller mutations. The dictionary
    interface remains mutable for compatibility; dataclass metadata is frozen.
    """
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise ValueError("delivery_interval_invalid")
    body = str(body or "").strip()
    segments = [str(segment).strip() for segment in text_segments or [] if str(segment).strip()]
    if segments and "\n\n".join(segments) != body:
        raise ValueError("delivery_text_segments_mismatch")
    texts = [DeliveryPart("text", content=segment) for segment in (segments or ([body] if body else []))]
    media = [DeliveryPart("media", material=deepcopy(item)) for item in materials]
    if delivery_mode == "assets_only":
        parts = media
    elif delivery_mode == "assets_then_text":
        parts = [*media, *texts]
    elif delivery_mode in {"text_only", "text_then_assets"}:
        parts = texts + ([] if delivery_mode == "text_only" else media)
    else:
        raise ValueError(f"delivery_mode_invalid:{delivery_mode}")
    question = str(follow_up_question or "").strip()
    if question:
        parts.append(DeliveryPart(
            "text", content=question, is_follow_up=True,
            follow_up_type=follow_up_type, follow_up_field=follow_up_field,
        ))
    # Hash only the delivery envelope, not transport-specific material objects.
    snapshot = [
        (part.kind, part.content, str((part.material or {}).get("media_hash") or ""),
         str((part.material or {}).get("asset_key")
         or (part.material or {}).get("key") or (part.material or {}).get("id")
         or (part.material or {}).get("url") or ""), part.is_follow_up)
        for part in parts
    ]
    fingerprint = sha256(json.dumps(
        [DELIVERY_PLAN_VERSION, plan_id, delivery_mode, content_group_key,
         interval_seconds, follow_up_group_key, follow_up_type, follow_up_field, snapshot],
        ensure_ascii=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return [replace(
        part,
        part_id=f"{fingerprint}:{index}",
        content_group_key=follow_up_group_key if part.is_follow_up else content_group_key,
        interval_seconds=interval_seconds if index else 0,
    ) for index, part in enumerate(parts)]
