"""Durable automation state. Rehearsal rows never reference the delivery outbox."""
from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class ReplyPolicy(Base):
    __tablename__ = "reply_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(100), unique=True)
    version: Mapped[int] = mapped_column(default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AutomationSession(Base):
    __tablename__ = "automation_sessions"
    __table_args__ = (Index('uq_v3_live_conversation', 'conversation_state_id', unique=True,
                           sqlite_where=text("environment = 'live'")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    __mapper_args__ = {"version_id_col": revision}
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    inbox_binding_id: Mapped[int | None] = mapped_column(ForeignKey("inbox_bindings.id"))
    conversation_state_id: Mapped[int | None] = mapped_column(ForeignKey("conversation_states.id"))
    mode: Mapped[str] = mapped_column(String(20), default="reply")
    environment: Mapped[str] = mapped_column(String(20), default="playground")
    engine_version: Mapped[str] = mapped_column(String(20), default="v3")
    engine_release_id: Mapped[str] = mapped_column(String(100), default="v3")
    generation: Mapped[int] = mapped_column(default=0)
    virtual_now: Mapped[str] = mapped_column(String(40), default=utcnow)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    memory: Mapped[dict] = mapped_column(JSON, default=dict)
    controls: Mapped[dict] = mapped_column(JSON, default=dict)
    batch_started_at: Mapped[str | None] = mapped_column(String(40))
    due_at: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AutomationRun(Base):
    __tablename__ = "automation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("automation_sessions.id"), index=True)
    module: Mapped[str] = mapped_column(String(20))
    generation: Mapped[int] = mapped_column()
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    policy_version: Mapped[int] = mapped_column(default=1)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    lease_token: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40))


class SopVersion(Base):
    __tablename__ = "sop_versions"
    __table_args__ = (UniqueConstraint("sop_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sop_id: Mapped[int] = mapped_column(ForeignKey("sop_definitions.id"))
    version: Mapped[int] = mapped_column()
    config: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class RehearsalEnrollment(Base):
    __tablename__ = "rehearsal_enrollments"
    __table_args__ = (
        UniqueConstraint("sop_id", "subject_key", "round_number", name="uq_sop_subject_round"),
        UniqueConstraint("sop_id", "subject_key", "request_key", name="uq_sop_subject_request"),
        Index("uq_sop_active_subject", "sop_id", "subject_key", unique=True, sqlite_where=text("status = 'active'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("automation_sessions.id"))
    sop_version_id: Mapped[int] = mapped_column(ForeignKey("sop_versions.id"))
    sop_id: Mapped[int] = mapped_column(ForeignKey("sop_definitions.id"))
    subject_key: Mapped[str] = mapped_column(String(160))
    round_number: Mapped[int] = mapped_column(default=1)
    request_key: Mapped[str | None] = mapped_column(String(80))
    trigger_source: Mapped[str] = mapped_column(String(30), default="manual")
    generation: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="active")
    enrolled_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))


class RehearsalJob(Base):
    __tablename__ = "rehearsal_jobs"
    __table_args__ = (UniqueConstraint("enrollment_id", "node_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("rehearsal_enrollments.id"))
    node_key: Mapped[str] = mapped_column(String(80))
    predecessor_id: Mapped[int | None] = mapped_column(ForeignKey("rehearsal_jobs.id"))
    scheduled_at: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(40), default="scheduled")
    reason: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class LiveSopEnrollment(Base):
    __tablename__ = "live_sop_enrollments"
    __table_args__ = (
        UniqueConstraint("sop_id", "subject_key", "round_number", name="uq_live_sop_subject_round"),
        UniqueConstraint("sop_id", "subject_key", "request_key", name="uq_live_sop_subject_request"),
        Index("uq_live_sop_active_subject", "sop_id", "subject_key", unique=True,
              sqlite_where=text("status = 'active'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    sop_id: Mapped[int] = mapped_column(ForeignKey("sop_definitions.id"))
    sop_version_id: Mapped[int] = mapped_column(ForeignKey("sop_versions.id"))
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    subject_key: Mapped[str] = mapped_column(String(160))
    round_number: Mapped[int] = mapped_column(default=1)
    request_key: Mapped[str] = mapped_column(String(80))
    trigger_source: Mapped[str] = mapped_column(String(30), default="manual_test")
    engine_version: Mapped[str] = mapped_column(String(20), default="v1")
    engine_release_id: Mapped[str] = mapped_column(String(100), default="v1")
    allow_repeat_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    enrolled_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))
    exit_reason: Mapped[str | None] = mapped_column(String(120))


class LiveSopJob(Base):
    __tablename__ = "live_sop_jobs"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "node_key", name="uq_live_sop_enrollment_node"),
        Index("ix_live_sop_jobs_due", "status", "scheduled_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("live_sop_enrollments.id"))
    node_key: Mapped[str] = mapped_column(String(80))
    predecessor_id: Mapped[int | None] = mapped_column(ForeignKey("live_sop_jobs.id"))
    scheduled_at: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="scheduled")
    reason: Mapped[str | None] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(default=0)
    confirmed_at: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40))


class WakeupPolicy(Base):
    __tablename__ = "wakeup_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    config: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class SilenceCycle(Base):
    __tablename__ = "silence_cycles"
    __table_args__ = (UniqueConstraint("session_id", "generation"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("automation_sessions.id"), index=True)
    generation: Mapped[int] = mapped_column()
    policy_id: Mapped[int | None] = mapped_column(ForeignKey("wakeup_policies.id"))
    policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    customer_at: Mapped[str] = mapped_column(String(40))
    reply_at: Mapped[str] = mapped_column(String(40))
    due_at: Mapped[str] = mapped_column(String(40), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="candidate")
    reason: Mapped[str | None] = mapped_column(String(120))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("automation_runs.id"))
    evaluation_count: Mapped[int] = mapped_column(default=0)


class TouchReservation(Base):
    __tablename__ = "touch_reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_key: Mapped[str] = mapped_column(String(120), unique=True)
    owner_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(40), default="reserved")
    reserved_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[str | None] = mapped_column(String(40))


class MaterialDelivery(Base):
    """Shared rehearsal ledger; never evidence of real Chatwoot delivery."""
    __tablename__ = "material_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_key: Mapped[str] = mapped_column(String(64), unique=True)
    subject_key: Mapped[str] = mapped_column(String(160), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("automation_sessions.id"))
    business_key: Mapped[str] = mapped_column(String(160))
    content_family: Mapped[str] = mapped_column(String(160))
    content_group_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    asset_hash: Mapped[str] = mapped_column(String(64))
    media_id: Mapped[int] = mapped_column(ForeignKey("stored_media.id"))
    source: Mapped[str] = mapped_column(String(30))
    route_variant: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(40), default="simulated_delivered")
    confirmed_at: Mapped[str] = mapped_column(String(40))


class SyncConversationCursor(Base):
    __tablename__ = "sync_conversation_cursors"
    __table_args__ = (UniqueConstraint("job_id", "conversation_state_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"))
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(120))
