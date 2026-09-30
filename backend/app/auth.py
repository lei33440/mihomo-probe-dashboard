"""JWT + bcrypt 鉴权。"""
from __future__ import annotations

import secrets
import time

import bcrypt
import jwt

from .config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def issue_token(username: str) -> tuple[str, int, str]:
    """返回 (token, expires_at_ts, jti)。

    jti 是随机 16 字节 hex，用于黑名单撤销。
    """
    exp_seconds = settings.jwt_expires_hours * 3600
    now = int(time.time())
    exp_at = now + exp_seconds
    jti = secrets.token_hex(16)
    payload = {
        "sub": username,
        "exp": exp_at,
        "iat": now,
        "jti": jti,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, exp_at, jti


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])