"""Runtime kill switch for every customer-facing Chatwoot message."""
from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.models import AppSetting


GLOBAL_MESSAGE_SETTING_KEY = "global_message_sending"


def global_message_sending_enabled(db: Session | None = None) -> bool:
    """Return the persisted switch value and fail closed when it is missing."""
    if db is not None:
        row = db.get(AppSetting, GLOBAL_MESSAGE_SETTING_KEY)
        return bool((row.value if row else {}).get("enabled", False))
    try:
        with SessionLocal() as fresh_db:
            row = fresh_db.get(AppSetting, GLOBAL_MESSAGE_SETTING_KEY)
            return bool((row.value if row else {}).get("enabled", False))
    except SQLAlchemyError:
        # A kill switch must fail closed if its persisted state cannot be read.
        return False
