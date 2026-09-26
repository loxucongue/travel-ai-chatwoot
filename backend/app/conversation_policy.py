from app.config import settings
from app.models import InboxBinding, Tenant, utcnow


AI_CONTROL_LABEL = "ai"


def has_ai_label(labels: list[str]) -> bool:
    return any(str(label).strip().casefold() == AI_CONTROL_LABEL for label in labels)


def observe_ai_label(row, labels: list[str], source: str) -> bool:
    """Apply an authoritative Chatwoot label observation to local desired state."""
    present = has_ai_label(labels)
    changed = present != bool(row.ai_label_present)
    if changed:
        row.ai_mode = "enabled" if present else "disabled"
        row.ai_mode_source = source
        row.ai_mode_updated_at = utcnow()
    row.ai_label_present = present
    if row.ai_mode == "enabled" and not present:
        row.ai_sync_status = "conflict"
    else:
        row.ai_sync_status = "synced"
    return changed


def compute_state(
    tenant: Tenant,
    inbox: InboxBinding,
    labels: list[str],
    contact_labels: list[str],
    can_reply: bool,
    ai_mode: str = "inherit",
    ai_sync_status: str = "synced",
    ai_label_present: bool | None = None,
) -> tuple[str, str]:
    if not tenant.ai_enabled:
        return "AI_PAUSED_GLOBAL", "tenant_disabled"
    if not inbox.ai_enabled:
        return "AI_PAUSED_INBOX", "inbox_disabled"
    for label in ("拒绝联系", "黑名单"):
        if label in contact_labels:
            return "AI_PAUSED_LABEL", f"contact_label:{label}"
    for label in ("客诉", "人工接管", "AI关闭"):
        if label in labels:
            state = "HUMAN_HANDOFF" if label in ("客诉", "人工接管") else "AI_PAUSED_LABEL"
            return state, f"conversation_label:{label}"
    if ai_sync_status != "synced":
        return "AI_PAUSED_SYNC", f"ai_label_sync:{ai_sync_status}"
    if ai_mode == "disabled":
        return "AI_PAUSED_CONVERSATION", "conversation_ai_disabled"
    if ai_mode == "enabled" and not ai_label_present:
        return "AI_PAUSED_SYNC", "ai_label_missing"
    if not has_ai_label(labels) or ai_mode != "enabled":
        return "AI_PAUSED_CONVERSATION", "ai_opt_in_required"
    if not can_reply:
        return "CHANNEL_BLOCKED", "can_reply_false"
    return "AI_ACTIVE", "inbox_enabled"
