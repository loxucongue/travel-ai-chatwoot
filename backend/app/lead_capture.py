"""Validated contact candidates with deterministic lead state transitions."""
from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.lead_capture_models import LeadCaptureState
from app.models import Contact, ConversationJourney, ConversationState, utcnow
from app.operations import setting_value
from app.route_packages import ROUTES


CONTACT_REQUEST = (
    "方便留一個 LINE 或 Email 給我嗎？"
    "我把完整行程整理給您，之後有問題隨時找我就好～"
)
CONTACT_ACKNOWLEDGEMENT = "謝謝您～聯絡方式我這邊已經記下來了，我幫您安排顧問接著跟進。"
LEGACY_CONTACT_REQUEST = (
    "為方便旅遊顧問依照您的人數與出發時間跟進，您願意留下方便聯絡的 "
    "LINE、微信、電話或 Email 嗎？提供其中一種即可。"
)
ALLOWED_CONTACT_CHANNELS = {"line", "wechat", "phone", "email", "whatsapp"}


def contact_request_for_route(route_variant: str) -> str:
    spec = ROUTES.get(route_variant, {})
    policy = spec.get("policies", {})
    key = policy.get("contact_request_group")
    return spec.get("groups", {}).get(key, {}).get("text") or CONTACT_REQUEST


CONTACT_REQUEST_TEXTS = {
    CONTACT_REQUEST,
    LEGACY_CONTACT_REQUEST,
    *(contact_request_for_route(route) for route in ROUTES),
}


def bind_lead_request(
    decision,
    *,
    route_variant: str,
    journey_stage: str,
    capture_status: str,
) -> tuple[object, bool]:
    """Enforce durable one-time capture state without rewriting model copy."""
    ask = (
        getattr(decision, "lead_action", "none") == "ask"
        and capture_status == "not_started"
        and getattr(decision, "action", "no_action") == "reply"
    )
    if getattr(decision, "lead_action", "none") == "ask" and not ask:
        decision.lead_action = "none"
    return decision, ask


@dataclass(frozen=True)
class ContactMatch:
    kind: str
    value: str
    masked: str


def _mask(kind: str, value: str) -> str:
    if kind == "email" and "@" in value:
        name, domain = value.split("@", 1)
        return f"{name[:1]}***@{domain}"
    if kind in {"phone", "whatsapp"}:
        digits = "".join(character for character in value if character.isdigit())
        return f"***{digits[-4:]}" if digits else "***"
    return f"{value[:2]}***{value[-2:]}" if len(value) > 4 else "***"


def valid_contact_value(kind: str, value: str) -> bool:
    if kind == "email":
        if len(value) > 254 or value.count("@") != 1:
            return False
        name, domain = value.split("@", 1)
        return bool(name and "." in domain and not domain.startswith(".") and not domain.endswith("."))
    if kind in {"phone", "whatsapp"}:
        digits = "".join(character for character in value if character.isdigit())
        return 8 <= len(digits) <= 15 and len(value) <= 32
    if kind in {"line", "wechat"}:
        # A channel name or a sentence such as "已加你 LINE" is not a
        # contact identifier. Accept only identifier-shaped values.
        if kind == "line" and re.fullmatch(r"https://line\.me/ti/p/[A-Za-z0-9_-]+", value, re.I):
            return True
        if value.casefold().lstrip("@") in {"line", "wechat", "weixin", "whatsapp"}:
            return False
        return bool(re.fullmatch(r"@?[A-Za-z0-9][A-Za-z0-9._-]{3,39}", value))
    return False


# Backward-compatible private name for older imports and tests.
_valid_contact_value = valid_contact_value


def model_contacts(decision, current_text: str) -> list[ContactMatch]:
    """Validate only contact values explicitly selected by the model."""
    source = str(current_text or "")
    if getattr(decision, "lead_action", "none") != "captured":
        return []
    found: list[ContactMatch] = []
    for kind, raw in (getattr(decision, "contact_values", {}) or {}).items():
        value = str(raw or "").strip()
        if kind not in ALLOWED_CONTACT_CHANNELS or not valid_contact_value(kind, value):
            continue
        if value not in source:
            continue
        if any(item.kind == kind and item.value.casefold() == value.casefold() for item in found):
            continue
        found.append(ContactMatch(kind=kind, value=value, masked=_mask(kind, value)))
    return found


def state_for(db: Session, conversation_id: int) -> LeadCaptureState:
    row = db.scalar(select(LeadCaptureState).where(
        LeadCaptureState.conversation_state_id == conversation_id
    ))
    if row is None:
        row = LeadCaptureState(conversation_state_id=conversation_id)
        db.add(row)
        db.flush()
    return row


def _known_contact_matches(contact: Contact | None) -> list[ContactMatch]:
    if contact is None:
        return []
    values: list[tuple[str, str]] = []
    if contact.email:
        values.append(("email", contact.email))
    if contact.phone_number:
        values.append(("phone", contact.phone_number))
    channels = (contact.custom_attributes or {}).get("lead_contacts") or {}
    if isinstance(channels, dict):
        values.extend(
            (str(kind), str(value))
            for kind, value in channels.items()
            if kind in ALLOWED_CONTACT_CHANNELS and str(value).strip()
        )
    return [ContactMatch(kind, value, _mask(kind, value)) for kind, value in values]


def _persist_contact(contact: Contact | None, matches: list[ContactMatch]) -> None:
    if contact is None:
        return
    attributes = dict(contact.custom_attributes or {})
    channels = dict(attributes.get("lead_contacts") or {})
    for item in matches:
        if item.kind == "email":
            contact.email = item.value
        elif item.kind in {"phone", "whatsapp"}:
            contact.phone_number = item.value
        else:
            channels[item.kind] = item.value
    if channels:
        attributes["lead_contacts"] = channels
        contact.custom_attributes = attributes
    contact.updated_at = utcnow()


def record_capture(
    db: Session,
    conversation: ConversationState,
    matches: list[ContactMatch],
    source_message_id: int | None,
) -> LeadCaptureState:
    row = state_for(db, conversation.id)
    row.status = "captured"
    row.captured_at = row.captured_at or utcnow()
    row.captured_kinds = sorted(set([*(row.captured_kinds or []), *(item.kind for item in matches)]))
    masked = dict(row.masked_values or {})
    masked.update({item.kind: item.masked for item in matches})
    row.masked_values = masked
    row.source_message_id = source_message_id or row.source_message_id
    row.updated_at = utcnow()
    _persist_contact(conversation.contact, matches)
    return row


def observe_history(
    db: Session,
    conversation: ConversationState,
    history: list[dict],
) -> LeadCaptureState:
    """Seed durable state only from structured data or our exact prior question."""
    row = state_for(db, conversation.id)
    if row.status == "captured":
        return row

    known = _known_contact_matches(conversation.contact)
    if known:
        return record_capture(db, conversation, known, None)

    mappings = setting_value(db, "label_mappings", {"lead_labels": ["已留资"]})
    lead_labels = set(mappings.get("lead_labels") or ["已留资"])
    if set(conversation.labels or []) & lead_labels:
        row.status = "captured"
        row.captured_at = row.captured_at or utcnow()
        row.updated_at = utcnow()
        return row

    if row.status == "not_started" and any(
        message.get("direction") == "outgoing"
        and any(
            request_text in str(message.get("content") or "")
            for request_text in CONTACT_REQUEST_TEXTS
        )
        for message in history
    ):
        row.status = "asked"
        row.request_count = max(1, row.request_count)
        row.updated_at = utcnow()
    return row


def apply_model_policy(
    db: Session,
    conversation: ConversationState,
    decision,
    history: list[dict],
    current_text: str,
) -> tuple[object, LeadCaptureState, bool, list[ContactMatch]]:
    """Apply model semantics while enforcing durable state and quoted evidence."""
    row = observe_history(db, conversation, history)
    matches = model_contacts(decision, current_text)

    if getattr(decision, "lead_action", "none") == "captured" and not matches:
        decision.lead_action = "none"
        decision.contact_values = {}
        decision.safety_flags = sorted(set([
            *(getattr(decision, "safety_flags", []) or []),
            "contact_value_not_in_current_message",
        ]))

    journey = db.scalar(select(ConversationJourney).where(
        ConversationJourney.conversation_state_id == conversation.id
    ))
    decision, ask = bind_lead_request(
        decision,
        route_variant=journey.route_variant if journey else "",
        journey_stage=journey.stage if journey else "route_selection",
        capture_status=row.status,
    )
    return decision, row, ask, matches


def mark_requested(row: LeadCaptureState) -> None:
    if row.status != "not_started":
        return
    row.status = "asked"
    row.request_count += 1
    row.requested_at = utcnow()
    row.updated_at = utcnow()


def capture_json(row: LeadCaptureState | None) -> dict:
    return {
        "status": row.status if row else "not_started",
        "request_count": row.request_count if row else 0,
        "requested_at": row.requested_at if row else None,
        "captured_at": row.captured_at if row else None,
        "captured_kinds": row.captured_kinds if row else [],
        "masked_values": row.masked_values if row else {},
        "label_sync_status": row.label_sync_status if row else "not_required",
    }
