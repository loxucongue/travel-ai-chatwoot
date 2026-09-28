from sqlalchemy.orm import Session
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.automation_models import ReplyPolicy
from app.operations import setting_value


DEFAULT_REPLY = {"enabled": True, "merge_wait_seconds": 2, "merge_max_seconds": 5, "backlog_seconds": 300}


BLOCK_LABELS = {"人工接管", "客诉", "客訴", "拒绝联系", "拒絕聯繫", "黑名单", "黑名單", "AI关闭", "ai_off", "已留资", "已留資", "已成交"}


def dt(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def reply_policy(db: Session, inbox_binding_id: int | None = None) -> tuple[dict, int]:
    global_row = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key == "global"))
    row = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key == f"inbox:{inbox_binding_id}")) if inbox_binding_id else None
    config = {**DEFAULT_REPLY, **(global_row.config if global_row else {}), **(row.config if row else {})}
    if global_row and not global_row.config.get("enabled", True):
        config["enabled"] = False
    return config, (row or global_row).version if (row or global_row) else 1


def blocking_labels(db: Session | None = None) -> set[str]:
    labels = set(BLOCK_LABELS)
    if db is not None:
        from app.operations import setting_value
        mappings = setting_value(db, "label_mappings", {})
        for key in ("handoff_labels", "lead_labels", "conversion_labels", "contact_block_labels"):
            labels.update(mappings.get(key, []))
    return labels
