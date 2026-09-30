"""登录相关。"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..auth import decode_token, issue_token, verify_password
from ..database import db_cursor
from ..repository import get_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ===== 简易内存限速：每 IP 5 次/分钟 =====
_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_LIMIT = 5
_LOGIN_WINDOW = 60.0


def _check_login_rate(ip: str) -> None:
    now = time.monotonic()
    arr = _login_attempts[ip]
    # 滑动窗口
    _login_attempts[ip] = [t for t in arr if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_LIMIT:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    _login_attempts[ip].append(now)


class LoginBody(BaseModel):
    username: str
    password: str


class LoginResp(BaseModel):
    token: str
    expires_at: int


@router.post("/login", response_model=LoginResp)
def login(body: LoginBody, request: Request) -> LoginResp:
    ip = request.client.host if request.client else "unknown"
    _check_login_rate(ip)
    admin = get_admin(body.username)
    if not admin or not verify_password(body.password, admin["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token, exp, jti = issue_token(body.username)
    return LoginResp(token=token, expires_at=exp)


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """主动登出：把当前 token 的 jti 加入撤销表。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except Exception:
        return {"status": "ok"}  # 无效 token 也算登出成功
    jti = payload.get("jti")
    if not jti:
        return {"status": "ok"}
    now_ms = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO revoked_tokens (jti, username, expires_at, revoked_at) VALUES (?, ?, ?, ?)",
            (jti, payload.get("sub", ""), payload.get("exp", 0) * 1000, now_ms),
        )
    return {"status": "ok"}


@router.post("/ensure-default")
def ensure_default_admin() -> dict[str, str]:
    from ..auth import hash_password
    from ..config import settings
    from ..repository import upsert_admin

    upsert_admin(settings.admin_username, hash_password(settings.admin_password))
    return {"status": "ok", "username": settings.admin_username}