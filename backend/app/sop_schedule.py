"""SOP schedule and ordered content contracts, shared by preview and rehearsal."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def relative_delay(node: dict) -> timedelta:
    """Return the configured relative delay with optional sub-minute precision."""
    return timedelta(
        minutes=int(node.get("delay_minutes") or 0),
        seconds=int(node.get("delay_seconds") or 0),
    )


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def content_items(node: dict) -> list[dict]:
    if node.get("messages") is not None:
        return node["messages"]
    return [{"key": "legacy", "content_type": node.get("content_type", "text"),
             "content": node.get("content", ""), "media_id": node.get("media_id")}]


def schedule_at(node: dict, *, customer_added_at: str | None, enrolled_at: str,
                last_customer_at: str | None = None, previous_at: str | None = None) -> str | None:
    kind = node.get("schedule_type", "relative")
    if kind == "fixed":
        result = timestamp(node["fixed_at"])
    elif kind == "calendar_day":
        anchor = enrolled_at if node.get("basis") == "enrollment" else customer_added_at
        if not anchor:
            raise ValueError("customer_added_time_missing")
        local = timestamp(anchor).astimezone(SHANGHAI)
        hour, minute = map(int, node["time_of_day"].split(":"))
        result = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=node["day_number"] - 1)
    else:
        basis = node.get("basis", "enrollment")
        anchor = {"customer_added": customer_added_at, "enrollment": enrolled_at,
                  "last_customer_reply": last_customer_at, "previous_node": previous_at}.get(basis)
        if not anchor:
            if basis == "previous_node":
                return None
            raise ValueError(f"{basis}_time_missing")
        result = timestamp(anchor) + relative_delay(node)
    return result.astimezone(timezone.utc).isoformat()


def schedule_preview(nodes: list[dict], added_at: str, frequency_hours: int) -> list[dict]:
    rows, previous = [], None
    start = timestamp(added_at)
    for index, node in enumerate(nodes):
        warnings = []
        try:
            due = schedule_at(node, customer_added_at=added_at, enrolled_at=added_at,
                              last_customer_at=added_at, previous_at=previous)
            if due is None:
                warnings.append("previous_message_required")
            else:
                moment = timestamp(due)
                if moment < start:
                    warnings.append("before_customer_added")
                if moment >= start + timedelta(hours=24, minutes=-5):
                    warnings.append("outside_initial_channel_window")
                if not 9 <= moment.astimezone(SHANGHAI).hour < 21:
                    warnings.append("outside_contact_hours")
                if previous and moment <= timestamp(previous):
                    warnings.append("not_after_previous_group")
                previous = due
        except (ValueError, KeyError, TypeError):
            due = None
            warnings.append("schedule_invalid")
        rows.append({"key": node["key"], "order": index + 1, "scheduled_at": due,
                     "message_count": len(content_items(node)), "warnings": warnings, "estimated": True})
    return rows
