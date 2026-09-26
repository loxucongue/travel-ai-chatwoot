"""Temporary local UI-test credentials, never a public bootstrap endpoint."""
import json
import secrets
import sys
from pathlib import Path
from sqlalchemy import select, update
from app.config import settings
from app.db import SessionLocal
from app.models import User, AppSession, SopDefinition, utcnow
from app.automation_models import WakeupPolicy
from app.security import hash_password

assert settings.app_profile == "evaluation" and not settings.outbound_enabled
with SessionLocal() as db:
    user=db.scalar(select(User).where(User.email.in_(["three-modules-qa@example.com","three-modules-qa@local.invalid"])))
    if "--revoke" in sys.argv:
        if user:
            user.active=False
            db.execute(update(AppSession).where(AppSession.user_id==user.id).values(revoked_at=utcnow()))
            db.execute(update(SopDefinition).where(SopDefinition.created_by==user.id,SopDefinition.name.like('QA-structure-only-%')).values(status='paused'))
            db.execute(update(WakeupPolicy).where(WakeupPolicy.created_by==user.id,WakeupPolicy.name.like('QA-wakeup-%')).values(status='paused'))
            db.commit()
        print("Temporary QA login revoked")
    else:
        password=secrets.token_urlsafe(24)
        if not user:
            user=User(email="three-modules-qa@example.com",display_name="QA",role="admin",password_hash=hash_password(password))
            db.add(user)
        user.active=True
        user.email="three-modules-qa@example.com"
        user.password_hash=hash_password(password)
        db.commit()
        target=Path("../.runlogs/three-qa-credentials.json")
        target.write_text(json.dumps({"email":user.email,"password":password}),encoding="utf-8")
        print("Temporary local QA login prepared")
