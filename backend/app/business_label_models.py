"""Durable, auditable business labels independent of AI reply ownership."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models import utcnow


class BusinessLabelState(Base):
    __tablename__ = 'business_label_states'
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey('conversation_states.id'), primary_key=True)
    consulted: Mapped[list] = mapped_column(JSON, default=list)
    current: Mapped[list] = mapped_column(JSON, default=list)
    rejected: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    engagement: Mapped[dict] = mapped_column(JSON, default=dict)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    managed_labels: Mapped[list] = mapped_column(JSON, default=list)
    remove_labels: Mapped[list] = mapped_column(JSON, default=list)
    last_message_id: Mapped[int] = mapped_column(default=0)
    sync_status: Mapped[str] = mapped_column(String(25), default='pending')
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    __mapper_args__ = {'version_id_col': revision}


class BusinessLabelJob(Base):
    __tablename__ = 'business_label_jobs'
    __table_args__ = (UniqueConstraint('conversation_state_id','message_id','config_version','mode'),
                     Index('ix_business_label_jobs_due','status','due_at'))
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey('conversation_states.id'))
    message_id: Mapped[int] = mapped_column(ForeignKey('message_events.id'))
    config_version: Mapped[int] = mapped_column(default=1)
    mode: Mapped[str] = mapped_column(String(20), default='live')
    status: Mapped[str] = mapped_column(String(25), default='pending')
    attempts: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    due_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)
