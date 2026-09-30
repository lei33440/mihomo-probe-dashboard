"""公开 API：只暴露 name / status / 最新延迟。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..repository import public_view

router = APIRouter(prefix="/api/public", tags=["public"])


class PublicProxy(BaseModel):
    id: int
    name: str
    group: str | None = None
    country: str | None = None
    type: str | None = None
    status: str
    delay: int | None = None
    last_check_ts: int | None = None
    last_error: str | None = None
    recent_delays: list[int] = []
    availability_24h: float | None = None


@router.get("/proxies", response_model=list[PublicProxy])
def get_public_proxies() -> list[PublicProxy]:
    return [PublicProxy(**p) for p in public_view()]


@router.get("/stats")
def public_stats() -> dict[str, int | float]:
    items = public_view()
    total = len(items)
    up = sum(1 for x in items if x["status"] == "up")
    delays = [x["delay"] for x in items if x["delay"] is not None]
    avg_delay = round(sum(delays) / len(delays), 1) if delays else 0
    return {
        "total": total,
        "up": up,
        "down": total - up,
        "avg_delay": avg_delay,
    }