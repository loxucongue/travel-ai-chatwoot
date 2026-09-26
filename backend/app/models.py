from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="china2go")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    analytics_cutover_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ChatwootConnection(Base):
    __tablename__ = "chatwoot_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), unique=True)
    base_url: Mapped[str] = mapped_column(String(500), default="https://app.chatwoot.com")
    account_id: Mapped[int] = mapped_column(Integer, default=180474, unique=True)
    encrypted_api_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    connection_key: Mapped[str] = mapped_column(String(96), unique=True)
    webhook_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="unconfigured")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_webhook_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class InboxBinding(Base):
    __tablename__ = "inbox_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_inbox_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chatwoot_inbox_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(250))
    channel_type: Mapped[str] = mapped_column(String(100), default="unknown")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    last_synced_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(50), default="super_admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    chatwoot_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class UserInboxScope(Base):
    __tablename__ = "user_inbox_scopes"
    __table_args__ = (UniqueConstraint("user_id", "inbox_binding_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    inbox_binding_id: Mapped[int] = mapped_column(ForeignKey("inbox_bindings.id"))
    scope_type: Mapped[str] = mapped_column(String(30), default="all")


class AppSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    revoked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    user: Mapped[User] = relationship()


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_pending", "status", "available_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("chatwoot_connections.id"))
    event: Mapped[str] = mapped_column(String(80))
    account_id: Mapped[int] = mapped_column(Integer)
    inbox_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(500), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    received_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    processed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_contact_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chatwoot_contact_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(250), default="Unknown")
    labels: Mapped[list] = mapped_column(JSON, default=list)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    custom_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ConversationState(Base):
    __tablename__ = "conversation_states"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_conversation_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    inbox_binding_id: Mapped[int] = mapped_column(ForeignKey("inbox_bindings.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    chatwoot_conversation_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="open")
    can_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    ai_mode: Mapped[str] = mapped_column(String(20), default="disabled")
    ai_engine_version: Mapped[str] = mapped_column(String(20), default="v1")
    ai_engine_release_id: Mapped[str] = mapped_column(String(100), default="v1")
    ai_mode_source: Mapped[str] = mapped_column(String(30), default="system")
    ai_label_present: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_sync_status: Mapped[str] = mapped_column(String(20), default="synced")
    ai_mode_updated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    effective_ai_state: Mapped[str] = mapped_column(String(50), default="AI_PAUSED_CONVERSATION")
    effective_state_reason: Mapped[str] = mapped_column(String(200), default="ai_opt_in_required")
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_customer_message_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignee_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    inbox: Mapped[InboxBinding] = relationship()
    contact: Mapped[Contact | None] = relationship()


class ConversationJourney(Base):
    """Durable, route-scoped progress for the live tourism assistant."""
    __tablename__ = "conversation_journeys"
    __table_args__ = (UniqueConstraint("conversation_state_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    knowledge_version_key: Mapped[str] = mapped_column(String(100), default="")
    route_variant: Mapped[str] = mapped_column(String(80), default="")
    stage: Mapped[str] = mapped_column(String(50), default="route_selection")
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_groups: Mapped[list] = mapped_column(JSON, default=list)
    last_group_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_trigger_message_id: Mapped[int | None] = mapped_column(ForeignKey("message_events.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class MessageEvent(Base):
    __tablename__ = "message_events"
    __table_args__ = (UniqueConstraint("conversation_state_id", "chatwoot_message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    chatwoot_message_id: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(30))
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    content_type: Mapped[str] = mapped_column(String(50), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attribution: Mapped[str] = mapped_column(String(40), default="unknown")
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    content_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AiRun(Base):
    __tablename__ = "ai_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    trigger_message_id: Mapped[int] = mapped_column(ForeignKey("message_events.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="started")
    action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    ai_run_id: Mapped[int | None] = mapped_column(ForeignKey("ai_runs.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), unique=True)
    content: Mapped[str] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="planned")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    submitted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), default="ai")
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(String(30), default="text")
    content_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatwootAgent(Base):
    __tablename__ = "chatwoot_agents"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_agent_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chatwoot_agent_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    availability_status: Mapped[str] = mapped_column(String(30), default="offline")
    role: Mapped[str] = mapped_column(String(40), default="agent")
    inbox_ids: Mapped[list] = mapped_column(JSON, default=list)
    last_synced_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ChatwootTeam(Base):
    __tablename__ = "chatwoot_teams"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_team_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chatwoot_team_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160))
    last_synced_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ChatwootLabel(Base):
    __tablename__ = "chatwoot_labels"
    __table_args__ = (UniqueConstraint("tenant_id", "chatwoot_label_id"), UniqueConstraint("tenant_id", "title"))
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chatwoot_label_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(20), default="#64748B")
    show_on_sidebar: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_sync_jobs_status", "status", "updated_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(String(40), default="history")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    phase: Mapped[str] = mapped_column(String(40), default="conversations")
    current_page: Mapped[int] = mapped_column(Integer, default=1)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    stable_passes: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class HandoffTask(Base):
    __tablename__ = "handoff_tasks"
    __table_args__ = (Index("ix_handoff_status_created", "status", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    reason_code: Mapped[str] = mapped_column(String(80), default="manual")
    reason_detail: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="P2")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    assignee_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assignment_target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assignment_target_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chatwoot_assignee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sla_due_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    claimed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class NotificationRead(Base):
    __tablename__ = "notification_reads"
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    read_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (Index("ix_notification_delivery_status", "status", "available_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    delivered_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SopDefinition(Base):
    __tablename__ = "sop_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_type: Mapped[str] = mapped_column(String(40), default="manual")
    trigger_labels: Mapped[list] = mapped_column(JSON, default=list)
    inbox_ids: Mapped[list] = mapped_column(JSON, default=list)
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    exit_labels: Mapped[list] = mapped_column(JSON, default=list)
    stop_on_incoming: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_hours: Mapped[int] = mapped_column(Integer, default=24)
    route_variant: Mapped[str] = mapped_column(String(60), default="")
    test_conversation_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class SopEnrollment(Base):
    __tablename__ = "sop_enrollments"
    __table_args__ = (UniqueConstraint("sop_id", "conversation_state_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sop_id: Mapped[int] = mapped_column(ForeignKey("sop_definitions.id"))
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    status: Mapped[str] = mapped_column(String(30), default="active")
    enrolled_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    exited_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SopJob(Base):
    __tablename__ = "sop_jobs"
    __table_args__ = (UniqueConstraint("enrollment_id", "node_key"), Index("ix_sop_jobs_due", "status", "scheduled_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("sop_enrollments.id"))
    node_key: Mapped[str] = mapped_column(String(80))
    scheduled_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="scheduled")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    skip_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("outbound_messages.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class StoredMedia(Base):
    __tablename__ = "stored_media"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    original_name: Mapped[str] = mapped_column(String(300))
    media_type: Mapped[str] = mapped_column(String(30))
    mime_type: Mapped[str] = mapped_column(String(120))
    file_size: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(600))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class LabelEvent(Base):
    __tablename__ = "label_events"
    __table_args__ = (Index("ix_label_events_created", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    label: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(20), default="added")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class KnowledgeVersion(Base):
    __tablename__ = "knowledge_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    version_key: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    source_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class WebKnowledgeSource(Base):
    __tablename__ = "web_knowledge_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "url"),
        Index("ix_web_knowledge_sources_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1200))
    description: Mapped[str] = mapped_column(Text, default="")
    match_keywords: Mapped[list] = mapped_column(JSON, default=list)
    auth_type: Mapped[str] = mapped_column(String(40), default="none")
    auth_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_scope: Mapped[str] = mapped_column(String(30), default="disabled")
    sync_status: Mapped[str] = mapped_column(String(30), default="never")
    latest_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_changed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class WebKnowledgeRevision(Base):
    __tablename__ = "web_knowledge_revisions"
    __table_args__ = (
        UniqueConstraint("source_id", "revision_number"),
        UniqueConstraint("source_id", "content_hash"),
        Index("ix_web_knowledge_revisions_source", "source_id", "revision_number"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("web_knowledge_sources.id"))
    revision_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), default="")
    final_url: Mapped[str] = mapped_column(String(1200))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_length: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending_review")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    fetched_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    script_blocks: Mapped[list] = mapped_column(JSON, default=list)
    image_candidates: Mapped[list] = mapped_column(JSON, default=list)


class RouteBranch(Base):
    __tablename__ = "route_branches"
    __table_args__ = (UniqueConstraint("knowledge_version_id", "branch_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_version_id: Mapped[int] = mapped_column(ForeignKey("knowledge_versions.id"))
    branch_key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    required_slots: Mapped[list] = mapped_column(JSON, default=list)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class RouteNode(Base):
    __tablename__ = "route_nodes"
    __table_args__ = (UniqueConstraint("route_branch_id", "node_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    route_branch_id: Mapped[int] = mapped_column(ForeignKey("route_branches.id"))
    node_key: Mapped[str] = mapped_column(String(80))
    node_type: Mapped[str] = mapped_column(String(40), default="reply")
    content: Mapped[str] = mapped_column(Text, default="")
    required_slots: Mapped[list] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    asset_keys: Mapped[list] = mapped_column(JSON, default=list)
    missing_content: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MaterialAsset(Base):
    __tablename__ = "material_assets"
    __table_args__ = (UniqueConstraint("knowledge_version_id", "asset_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_version_id: Mapped[int] = mapped_column(ForeignKey("knowledge_versions.id"))
    asset_key: Mapped[str] = mapped_column(String(120))
    source_path: Mapped[str] = mapped_column(String(700))
    display_name: Mapped[str] = mapped_column(String(300))
    media_type: Mapped[str] = mapped_column(String(40), default="image")
    usage: Mapped[str] = mapped_column(String(200), default="")
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class EvaluationDataset(Base):
    __tablename__ = "evaluation_datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    knowledge_version_id: Mapped[int] = mapped_column(ForeignKey("knowledge_versions.id"))
    name: Mapped[str] = mapped_column(String(200))
    filter_version: Mapped[str] = mapped_column(String(80))
    filter_config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="building", index=True)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("dataset_id", "case_key"), Index("ix_evaluation_cases_dataset", "dataset_id", "id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("evaluation_datasets.id"))
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    case_key: Mapped[str] = mapped_column(String(100))
    target_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    customer_text: Mapped[str] = mapped_column(Text)
    context_messages: Mapped[list] = mapped_column(JSON, default=list)
    reference_answer: Mapped[str] = mapped_column(Text, default="")
    expected_branch: Mapped[str | None] = mapped_column(String(60), nullable=True)
    expected_handoff: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("evaluation_datasets.id"))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    completed_cases: Mapped[int] = mapped_column(Integer, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (UniqueConstraint("run_id", "case_id"), Index("ix_evaluation_results_run", "run_id", "status"))
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id"))
    case_id: Mapped[int] = mapped_column(ForeignKey("evaluation_cases.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(60), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_slots: Mapped[list] = mapped_column(JSON, default=list)
    handoff_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    safety_flags: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    automatic_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"
    __table_args__ = (Index("ix_model_call_logs_result", "evaluation_result_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_result_id: Mapped[int] = mapped_column(ForeignKey("evaluation_results.id"))
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    response_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


# Register additive automation tables for Alembic and isolated test databases.
from app import automation_models as _automation_models  # noqa: E402,F401
from app import live_reply_models as _live_reply_models  # noqa: E402,F401
from app import lead_capture_models as _lead_capture_models  # noqa: E402,F401
