"""Rebuildable reporting records. Never grant reception or send permissions."""
from sqlalchemy import ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models import utcnow


class BiConversation(Base):
    __tablename__ = 'bi_conversations'
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey('conversation_states.id'), primary_key=True)
    source_created_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    signature: Mapped[str] = mapped_column(String(64), default='')
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    refreshed_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class BiEvent(Base):
    __tablename__ = 'bi_events'
    __table_args__ = (Index('ix_bi_event_time_kind', 'occurred_at', 'kind'),
                     Index('ix_bi_event_conversation', 'conversation_state_id', 'occurred_at'))
    key: Mapped[str] = mapped_column(String(250), primary_key=True)
    conversation_state_id: Mapped[int] = mapped_column(ForeignKey('conversation_states.id'))
    kind: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class BiRefresh(Base):
    __tablename__ = 'bi_refresh'
    id: Mapped[int] = mapped_column(primary_key=True)
    refreshed_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    remaining: Mapped[int] = mapped_column(default=0)
