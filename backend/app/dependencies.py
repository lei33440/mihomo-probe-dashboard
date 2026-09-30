"""FastAPI 通用依赖：从 Authorization 头提取 JWT。"""
from __future__ import annotations

from fastapi import Header, HTTPException, status

from .auth import decode_token
from .database import db_cursor


async def require_admin(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        )
    # 黑名单检查
    jti = payload.get("jti")
    if jti:
        with db_cursor() as cur:
            cur.execute("SELECT 1 FROM revoked_tokens WHERE jti=?", (jti,))
            if cur.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="token revoked",
                )
    return payload["sub"]