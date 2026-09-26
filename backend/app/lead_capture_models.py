from sqlalchemy import ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class LeadCaptureState(Base):
    __tablename__ = "lead_capture_states"
    __table_args__ = (Index("ix_lead_capture_status_updated", "status", "updated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_states.id"), unique=True
    )
    status: Mapped[str] = mapped_column(String(30), default="not_started")
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    captured_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    captured_kinds: Mapped[list] = mapped_column(JSON, default=list)
    masked_values: Mapped[dict] = mapped_column(JSON, default=dict)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_events.id"), nullable=True
    )
    label_sync_status: Mapped[str] = mapped_column(String(30), default="not_required")
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)
