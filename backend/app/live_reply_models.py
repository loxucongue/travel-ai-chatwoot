"""Persistent jobs for opt-in customer replies, separate from rehearsal jobs."""
from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class LiveReplyJob(Base):
    __tablename__ = "live_reply_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey("conversation_states.id"), index=True)
    trigger_message_id: Mapped[int] = mapped_column(ForeignKey("message_events.id"), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    engine_version: Mapped[str] = mapped_column(String(20), default="v1")
    engine_release_id: Mapped[str] = mapped_column(String(100), default="v1")
    input_ids: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(160))
    due_at: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_at: Mapped[str | None] = mapped_column(String(40))
