"""Dry-run the production impact of LIVE_SOP_SCOPE=ai_label.

This script is intentionally read-only. It does not call Chatwoot and does not
write to the local database. It answers two production rollout questions:

1. Which conversations would be eligible for AI replies on the next customer
   message if operators use the remote Chatwoot "ai" label?
2. Are there any existing live SOP enrollments/jobs outside the allowlist that
   could send after switching the scope?
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_HANDOFF_STATUSES = {"pending", "claimed"}
ACTIVE_ENROLLMENT_STATUSES = {"active"}
LIVE_JOB_STATUSES = {"scheduled", "retry", "blocked"}
DEFAULT_BLOCK_LABELS = {
    "人工接管", "客诉", "客訴", "拒绝联系", "拒絕聯繫", "黑名单", "黑名單",
    "AI关闭", "ai_off", "已留资", "已留資", "已成交",
}
DEFAULT_LABEL_MAPPINGS = {
    "handoff_labels": ["人工接管", "客诉"],
    "contact_block_labels": ["拒绝联系", "黑名单"],
    "lead_labels": ["已留资"],
    "conversion_labels": ["已成交"],
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def age_hours(value: str | None, now: datetime) -> float | None:
    dt = parse_time(value)
    if not dt:
        return None
    return round((now - dt).total_seconds() / 3600, 2)


def load_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def short(value: str | None, limit: int = 80) -> str:
    text = (value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_rows(conn: sqlite3.Connection, live_inbox_id: int | None) -> list[sqlite3.Row]:
    where = ""
    params: list[Any] = []
    if live_inbox_id:
        where = "WHERE ib.chatwoot_inbox_id = ?"
        params.append(live_inbox_id)
    return list(conn.execute(
        f"""
        SELECT
            cs.id AS local_id,
            cs.chatwoot_conversation_id AS conversation_id,
            cs.status,
            cs.can_reply,
            cs.labels,
            cs.ai_mode,
            cs.ai_label_present,
            cs.ai_sync_status,
            cs.effective_ai_state,
            cs.effective_state_reason,
            cs.last_customer_message_at,
            cs.last_message,
            cs.assignee_name,
            cs.team_name,
            ib.chatwoot_inbox_id,
            ib.name AS inbox_name,
            ib.channel_type,
            ib.ai_enabled AS inbox_ai_enabled,
            c.name AS contact_name,
            c.labels AS contact_labels,
            cj.route_variant,
            cj.stage,
            cj.slots
        FROM conversation_states cs
        JOIN inbox_bindings ib ON ib.id = cs.inbox_binding_id
        LEFT JOIN contacts c ON c.id = cs.contact_id
        LEFT JOIN conversation_journeys cj ON cj.conversation_state_id = cs.id
        {where}
        ORDER BY cs.chatwoot_conversation_id
        """,
        params,
    ))


def active_handoff_ids(conn: sqlite3.Connection) -> set[int]:
    placeholders = ",".join("?" for _ in ACTIVE_HANDOFF_STATUSES)
    return {
        int(row["conversation_state_id"])
        for row in conn.execute(
            f"SELECT conversation_state_id FROM handoff_tasks WHERE status IN ({placeholders})",
            list(ACTIVE_HANDOFF_STATUSES),
        )
    }


def configured_blocking_labels(conn: sqlite3.Connection) -> set[str]:
    labels = set(DEFAULT_BLOCK_LABELS)
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'label_mappings'").fetchone()
    mappings = {**DEFAULT_LABEL_MAPPINGS, **load_json(row["value"], {})} if row else DEFAULT_LABEL_MAPPINGS
    for key in ("handoff_labels", "lead_labels", "conversion_labels", "contact_block_labels"):
        labels.update(str(item) for item in mappings.get(key, []) if str(item).strip())
    return labels


def live_sop_risk(conn: sqlite3.Connection, allowlist: set[int]) -> dict[str, Any]:
    placeholders_enrollment = ",".join("?" for _ in ACTIVE_ENROLLMENT_STATUSES)
    placeholders_jobs = ",".join("?" for _ in LIVE_JOB_STATUSES)
    rows = list(conn.execute(
        f"""
        SELECT
            e.id AS enrollment_id,
            e.status AS enrollment_status,
            e.trigger_source,
            e.enrolled_at,
            cs.chatwoot_conversation_id AS conversation_id,
            cs.ai_label_present,
            cs.effective_ai_state,
            sv.config AS sop_config,
            COUNT(j.id) AS pending_jobs
        FROM live_sop_enrollments e
        JOIN conversation_states cs ON cs.id = e.conversation_state_id
        JOIN sop_versions sv ON sv.id = e.sop_version_id
        LEFT JOIN live_sop_jobs j ON j.enrollment_id = e.id AND j.status IN ({placeholders_jobs})
        WHERE e.status IN ({placeholders_enrollment})
        GROUP BY e.id
        ORDER BY e.id
        """,
        list(LIVE_JOB_STATUSES) + list(ACTIVE_ENROLLMENT_STATUSES),
    ))
    risky = []
    for row in rows:
        config = load_json(row["sop_config"], {})
        configured = set(int(x) for x in config.get("test_conversation_ids") or [] if str(x).isdigit())
        conversation_id = int(row["conversation_id"])
        outside_current_allowlist = conversation_id not in allowlist or conversation_id not in configured
        if outside_current_allowlist and int(row["pending_jobs"] or 0) > 0:
            risky.append({
                "conversation_id": conversation_id,
                "enrollment_id": int(row["enrollment_id"]),
                "pending_jobs": int(row["pending_jobs"]),
                "ai_label_present": bool(row["ai_label_present"]),
                "effective_ai_state": row["effective_ai_state"],
                "trigger_source": row["trigger_source"],
                "enrolled_at": row["enrolled_at"],
            })
    return {
        "active_enrollments": len(rows),
        "outside_allowlist_with_pending_jobs": risky,
    }


def classify_candidate(row: sqlite3.Row, active_handoffs: set[int], block_labels: set[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not bool(row["ai_label_present"]):
        reasons.append("no_ai_label")
    if row["ai_mode"] != "enabled":
        reasons.append(f"ai_mode_{row['ai_mode']}")
    if row["ai_sync_status"] != "synced":
        reasons.append(f"ai_sync_{row['ai_sync_status']}")
    if row["effective_ai_state"] != "AI_ACTIVE":
        reasons.append(f"effective_{row['effective_ai_state']}")
    if not bool(row["can_reply"]):
        reasons.append("channel_cannot_reply")
    if not bool(row["inbox_ai_enabled"]):
        reasons.append("inbox_ai_disabled")
    if int(row["local_id"]) in active_handoffs:
        reasons.append("active_handoff")
    labels = [str(x) for x in load_json(row["labels"], [])]
    contact_labels = [str(x) for x in load_json(row["contact_labels"], [])]
    for label in sorted(set(labels + contact_labels) & block_labels):
        reasons.append(f"blocking_label_{label}")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/backups/prod-current-20260901-200222.db",
                        help="Path to a production snapshot SQLite database.")
    parser.add_argument("--live-inbox-id", type=int, default=128859,
                        help="Chatwoot inbox ID to evaluate. Use 0 to include all inboxes.")
    parser.add_argument("--allowlist", default="26",
                        help="Current allowlist conversation IDs, comma separated.")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--json-output", default="")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    live_inbox_id = args.live_inbox_id or None
    allowlist = {int(x.strip()) for x in args.allowlist.split(",") if x.strip().isdigit()}
    now = datetime.now(timezone.utc)

    with open_db(db_path) as conn:
        rows = fetch_rows(conn, live_inbox_id)
        active_handoffs = active_handoff_ids(conn)
        block_labels = configured_blocking_labels(conn)
        eligible = []
        blocked = []
        reason_counts: Counter[str] = Counter()
        for row in rows:
            ok, reasons = classify_candidate(row, active_handoffs, block_labels)
            item = {
                "conversation_id": int(row["conversation_id"]),
                "contact_name": row["contact_name"],
                "status": row["status"],
                "can_reply": bool(row["can_reply"]),
                "ai_label_present": bool(row["ai_label_present"]),
                "labels": load_json(row["labels"], []),
                "contact_labels": load_json(row["contact_labels"], []),
                "ai_mode": row["ai_mode"],
                "effective_ai_state": row["effective_ai_state"],
                "last_customer_message_at": row["last_customer_message_at"],
                "last_customer_message_age_hours": age_hours(row["last_customer_message_at"], now),
                "route_variant": row["route_variant"],
                "stage": row["stage"],
                "last_message": short(row["last_message"]),
            }
            if ok:
                eligible.append(item)
            else:
                for reason in reasons:
                    reason_counts[reason] += 1
                blocked.append({**item, "blocked_reasons": reasons})
        risk = live_sop_risk(conn, allowlist)

    report = {
        "database": str(db_path),
        "live_inbox_id": live_inbox_id,
        "scope_if_enabled": "ai_label",
        "important_semantics": {
            "does_not_reply_to_old_messages": True,
            "ai_reply_trigger": "next incoming message on a remote Chatwoot conversation with ai label",
            "sop_trigger": "successful AI reply with a supported route_variant",
        },
        "counts": {
            "evaluated_conversations": len(rows),
            "ai_label_next_incoming_eligible": len(eligible),
            "blocked_or_not_opted_in": len(blocked),
            "active_handoff_conversations": len(active_handoffs),
            "blocking_labels_configured": len(block_labels),
        },
        "blocked_reason_counts": dict(reason_counts.most_common()),
        "existing_live_sop_risk": risk,
        "eligible_sample": eligible[: args.sample_size],
    }

    if args.json_output:
        output_path = Path(args.json_output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if risk["outside_allowlist_with_pending_jobs"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
