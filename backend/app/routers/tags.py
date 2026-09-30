"""tags CRUD + 节点打/解标签路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import require_admin
from ..repository import (
    add_tag_to_proxies,
    bulk_get_proxy_tags,
    create_tag,
    delete_tag,
    get_proxy_tags,
    get_tag,
    get_tag_by_name,
    list_tags_with_count,
    set_proxy_tags,
    update_tag,
)

router = APIRouter(
    prefix="/api/admin/tags",
    tags=["tags"],
    dependencies=[Depends(require_admin)],
)


class CreateTagBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    color: str = Field(default="#58a6ff", pattern=r"^#[0-9a-fA-F]{6}$")


class UpdateTagBody(BaseModel):
    name: str | None = None
    color: str | None = None


class SetProxyTagsBody(BaseModel):
    tag_ids: list[int] = []


class BatchTagBody(BaseModel):
    proxy_ids: list[int]
    tag_ids: list[int]


@router.get("")
def list_tags() -> list[dict[str, Any]]:
    return list_tags_with_count()


@router.post("")
def create(body: CreateTagBody) -> dict[str, Any]:
    if get_tag_by_name(body.name):
        raise HTTPException(status_code=409, detail=f"标签 {body.name!r} 已存在")
    tid = create_tag(body.name, body.color)
    return {"id": tid, "name": body.name, "color": body.color, "proxy_count": 0}


@router.patch("/{tid}")
def update(tid: int, body: UpdateTagBody) -> dict[str, str]:
    if not get_tag(tid):
        raise HTTPException(status_code=404, detail="标签不存在")
    update_tag(tid, name=body.name, color=body.color)
    return {"status": "ok"}


@router.delete("/{tid}")
def delete(tid: int) -> dict[str, str]:
    delete_tag(tid)
    return {"status": "ok"}


# ===== 节点 ↔ 标签 =====

@router.put("/proxies/{cid}/tags")
def set_tags_for_proxy(cid: int, body: SetProxyTagsBody) -> dict[str, Any]:
    # 校验所有 tag 都存在
    for tid in body.tag_ids:
        if not get_tag(tid):
            raise HTTPException(status_code=400, detail=f"标签 id={tid} 不存在")
    set_proxy_tags(cid, body.tag_ids)
    return {"status": "ok", "tags": get_proxy_tags(cid)}


@router.get("/proxies/{cid}/tags")
def get_tags_of_proxy(cid: int) -> list[dict[str, Any]]:
    return get_proxy_tags(cid)


@router.post("/batch-apply")
def batch_apply(body: BatchTagBody) -> dict[str, Any]:
    """给一批节点打一批标签。"""
    if not body.proxy_ids or not body.tag_ids:
        return {"affected": 0}
    n = add_tag_to_proxies(body.proxy_ids, body.tag_ids)
    return {"affected": n, "applied_to": len(body.proxy_ids), "tags": body.tag_ids}