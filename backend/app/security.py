import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from app.config import settings

password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except Exception:
        return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(32)


def session_csrf_token(session_token: str) -> str:
    # Domain-separated, stable per session; never return the session cookie itself.
    return hmac.new(settings.app_secret_key.encode(),
                    ("csrf-v1:" + session_token).encode(), hashlib.sha256).hexdigest()


def expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)).isoformat()


def get_fernet() -> Fernet:
    if settings.app_encryption_key:
        key = settings.app_encryption_key.encode()
    else:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> bytes:
    return get_fernet().encrypt(value.encode())


def decrypt_secret(value: bytes) -> str:
    return get_fernet().decrypt(value).decode()
