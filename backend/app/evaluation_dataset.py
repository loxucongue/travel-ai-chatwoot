from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_knowledge import FILTER_VERSION, PEACH_KEYWORDS, contains_peach_topic, deterministic_branch, deterministic_guard, seed_business_knowledge
from app.models import ConversationState, EvaluationCase, EvaluationDataset, InboxBinding, MessageEvent, Tenant, User, utcnow
from app.reply_context import public_context


@dataclass(frozen=True)
class Turn:
    target: list[MessageEvent]
    context: list[MessageEvent]
    reference: list[MessageEvent]


def _public_messages(db: Session, conversation_id: int) -> list[MessageEvent]:
    return list(db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_state_id == conversation_id,
        MessageEvent.private.is_(False),
        MessageEvent.direction.in_(["incoming", "outgoing"]),
    ).order_by(MessageEvent.created_at, MessageEvent.id)).all())


def _target_turns(messages: list[MessageEvent], all_topics: bool = False) -> list[Turn]:
    turns: list[Turn] = []
    index = 0
    while index < len(messages):
        if messages[index].direction != "incoming":
            index += 1
            continue
        start = index
        target: list[MessageEvent] = []
        while index < len(messages) and messages[index].direction == "incoming":
            row = messages[index]
            if row.content_type == "text" and row.content.strip():
                target.append(row)
            index += 1
        reference: list[MessageEvent] = []
        cursor = index
        while cursor < len(messages) and messages[cursor].direction == "outgoing":
            row = messages[cursor]
            if row.content_type == "text" and row.content.strip():
                reference.append(row)
            cursor += 1
        if target and (all_topics or contains_peach_topic("\n".join(row.content for row in target))):
            turns.append(Turn(target=target, context=messages[:start], reference=reference))
    return turns


def build_dataset(db: Session, user: User, name: str, inbox_id: int, module: str = "reply", all_topics: bool = False, exclude_conversation_ids: list[int] | None = None, history_verified: bool = False) -> EvaluationDataset:
    tenant = db.scalar(select(Tenant))
    knowledge = seed_business_knowledge(db)
    dataset = EvaluationDataset(
        tenant_id=tenant.id,
        knowledge_version_id=knowledge.id,
        name=name,
        filter_version="all-public-turns-full-v3" if all_topics else FILTER_VERSION,
        filter_config={"keywords": [] if all_topics else PEACH_KEYWORDS, "inbox_id": inbox_id, "public_text_only": True, "max_context_messages": None, "context_complete": history_verified, "module": module, "permission_state": "simulated_historical", "silence_hours": 2},
        created_by=user.id,
        snapshot_at=utcnow(),
    )
    db.add(dataset)
    db.flush()
    inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id, InboxBinding.chatwoot_inbox_id == inbox_id))
    if not inbox:
        dataset.status = "failed"
        raise ValueError("evaluation_inbox_not_found")

    conversations = db.scalars(select(ConversationState).where(ConversationState.tenant_id == tenant.id, ConversationState.inbox_binding_id == inbox.id).order_by(ConversationState.id)).all()
    count = 0
    exclusions = Counter()
    all_message_ids = []
    for conversation in conversations:
        if conversation.chatwoot_conversation_id in (exclude_conversation_ids or []):
            exclusions["test_conversation"] += 1
            continue
        messages = _public_messages(db, conversation.id)
        all_message_ids.extend((x.chatwoot_message_id, x.content, x.created_at) for x in messages)
        for row in db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == conversation.id)).all():
            if row.private: exclusions["private_message"] += 1
            elif row.direction == "activity": exclusions["system_activity"] += 1
            elif row.direction == "incoming" and row.content_type != "text": exclusions["attachment_only"] += 1
            elif row.direction == "incoming" and not row.content.strip(): exclusions["empty_incoming"] += 1
        for turn in _target_turns(messages, all_topics):
            ids = [row.chatwoot_message_id for row in turn.target]
            case_key = hashlib.sha256(f"{conversation.chatwoot_conversation_id}:{','.join(map(str, ids))}".encode()).hexdigest()[:40]
            customer_text = "\n".join(row.content.strip() for row in turn.target)
            if all_topics and any(x in customer_text for x in ["测试人员触发", "測試人員觸發", "relay-e2e-"]):
                exclusions["test_turn"] += 1
                continue
            related_ids = [conversation.id]
            if conversation.contact_id:
                related_ids = db.scalars(select(ConversationState.id).where(
                    ConversationState.tenant_id == conversation.tenant_id,
                    ConversationState.inbox_binding_id == conversation.inbox_binding_id,
                    ConversationState.contact_id == conversation.contact_id)).all()
            related = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id.in_(related_ids))).all()
            context = public_context([{"id": row.chatwoot_message_id, "conversation_id": row.conversation_state_id,
                "direction": row.direction, "private": row.private, "content": row.content,
                "attachments": row.attachments, "created_at": row.created_at} for row in related],
                {"id": turn.target[0].chatwoot_message_id, "created_at": turn.target[0].created_at})
            reference = "\n".join(row.content.strip() for row in turn.reference)
            if module == "wakeup":
                confirmed = [row for row in turn.reference if row.status in ("sent", "delivered", "read")]
                if not confirmed:
                    exclusions["no_confirmed_reply"] += 1
                    continue
                anchor = confirmed[-1]
                at = datetime.fromisoformat(anchor.created_at.replace("Z", "+00:00")) + timedelta(hours=2)
                if at.isoformat() > dataset.snapshot_at:
                    exclusions["not_yet_due"] += 1
                    continue
                if any(row.direction == "incoming" and anchor.created_at < row.created_at <= at.isoformat() for row in messages):
                    exclusions["customer_replied_before_wakeup"] += 1
                    continue
                context = [{"id": row.chatwoot_message_id, "direction": row.direction, "content_type": row.content_type, "content": row.content, "created_at": row.created_at} for row in messages if row.created_at <= anchor.created_at]
                context.append({"direction": "evaluation_clock", "created_at": at.isoformat(), "content": "Historical permissions are simulated; no real delivery."})
                reference = ""
            branch = None if all_topics else deterministic_branch(customer_text)
            db.add(EvaluationCase(
                dataset_id=dataset.id,
                conversation_state_id=conversation.id,
                case_key=case_key,
                target_message_ids=ids,
                customer_text=customer_text,
                context_messages=context,
                reference_answer=reference,
                expected_branch=branch,
                expected_handoff=None if all_topics else deterministic_guard(customer_text, branch)[0],
            ))
            count += 1
    dataset.case_count = count
    dataset.filter_config = {**dataset.filter_config, "conversation_count": len(conversations), "snapshot_hash": hashlib.sha256(json.dumps(all_message_ids, ensure_ascii=False).encode()).hexdigest(), "exclusions": dict(exclusions)}
    dataset.status = "ready"
    db.flush()
    return dataset
