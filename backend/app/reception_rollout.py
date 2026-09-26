"""Persisted live AI rollout scope shared by every delivery boundary."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting


AI_RECEPTION_ROLLOUT_KEY = "ai_reception_rollout"


def default_engine_assignment() -> dict[str, str]:
    """Return the engine stamped on newly mirrored conversations.

    Existing conversations keep their persisted engine, so V1/V2 can still be
    selected per conversation while operations controls the default for new
    traffic through AI_ENGINE_DEFAULT.
    """
    if settings.ai_engine_default == "v2":
        from app.reception_v2 import ENGINE_RELEASE_ID

        return {
            "ai_engine_version": "v2",
            "ai_engine_release_id": ENGINE_RELEASE_ID,
        }
    return {"ai_engine_version": "v1", "ai_engine_release_id": "v1"}


@dataclass(frozen=True)
class ReceptionRollout:
    allowlist_enabled: bool
    conversation_ids: frozenset[int]

    @property
    def scope(self) -> str:
        return "allowlist" if self.allowlist_enabled else "ai_label"

    def allows(self, conversation_id: int) -> bool:
        return not self.allowlist_enabled or conversation_id in self.conversation_ids

    def to_dict(self) -> dict:
        return {
            "allowlist_enabled": self.allowlist_enabled,
            "conversation_ids": sorted(self.conversation_ids),
            "scope": self.scope,
        }


def _environment_default() -> ReceptionRollout:
    return ReceptionRollout(
        allowlist_enabled=settings.live_sop_scope == "allowlist",
        conversation_ids=frozenset(settings.live_sop_allowlist),
    )


def _normalize(value: object, *, fallback: ReceptionRollout) -> ReceptionRollout:
    if not isinstance(value, dict):
        return fallback
    enabled = value.get("allowlist_enabled")
    raw_ids = value.get("conversation_ids")
    if not isinstance(enabled, bool) or not isinstance(raw_ids, list):
        return fallback
    ids: set[int] = set()
    for item in raw_ids:
        if isinstance(item, bool):
            return fallback
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            return fallback
        if parsed <= 0:
            return fallback
        ids.add(parsed)
    return ReceptionRollout(enabled, frozenset(ids))


def reception_rollout(db: Session | None = None) -> ReceptionRollout:
    """Return the effective scope; database errors preserve the narrow env default."""
    fallback = _environment_default()
    if db is not None:
        row = db.get(AppSetting, AI_RECEPTION_ROLLOUT_KEY)
        return _normalize(row.value if row else None, fallback=fallback)
    try:
        with SessionLocal() as fresh_db:
            row = fresh_db.get(AppSetting, AI_RECEPTION_ROLLOUT_KEY)
            return _normalize(row.value if row else None, fallback=fallback)
    except SQLAlchemyError:
        return fallback


def reception_conversation_allowed(db: Session, conversation_id: int) -> bool:
    return reception_rollout(db).allows(conversation_id)
