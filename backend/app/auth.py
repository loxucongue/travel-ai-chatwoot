from datetime import datetime, timezone
from secrets import compare_digest

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import AppSession, User
from app.security import digest, session_csrf_token


def current_user(request: Request, aiops_session: str | None = Cookie(default=None), db: Session = Depends(get_db)) -> User:
    if not aiops_session:
        raise HTTPException(401, detail={"code": "authentication_required", "message": "请先登录"})
    session = db.scalar(select(AppSession).where(AppSession.session_hash == digest(aiops_session), AppSession.revoked_at.is_(None)))
    if not session or session.expires_at <= datetime.now(timezone.utc).isoformat() or not session.user.active:
        raise HTTPException(401, detail={"code": "session_expired", "message": "登录已过期"})
    if session.user.must_change_password and request.scope["route"].name not in {
        "me", "csrf", "change_password", "logout",
    }:
        raise HTTPException(403, detail={"code": "password_change_required", "message": "请先修改临时密码"})
    return session.user


def require_csrf(
    request: Request,
    user: User = Depends(current_user),
    aiops_session: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    origin = request.headers.get("origin")
    if origin and origin not in settings.origins:
        raise HTTPException(403, detail={"code": "origin_not_allowed", "message": "请求来源不允许"})
    session = db.scalar(select(AppSession).where(AppSession.session_hash == digest(aiops_session or ""), AppSession.revoked_at.is_(None)))
    if not session or not x_csrf_token or not (
        compare_digest(session.csrf_hash, digest(x_csrf_token))
        or compare_digest(digest(session_csrf_token(aiops_session or "")), digest(x_csrf_token))
    ):
        raise HTTPException(403, detail={"code": "csrf_invalid", "message": "CSRF 校验失败"})
    return user


def super_admin(user: User = Depends(current_user)) -> User:
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "权限不足"})
    return user


def require_super_admin_csrf(user: User = Depends(require_csrf)) -> User:
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "权限不足"})
    return user
