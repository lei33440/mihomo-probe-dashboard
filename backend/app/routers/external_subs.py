"""external_subscriptions 路由：CRUD + 立即同步。"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator


# ===== SSRF 防护：拒绝私网 / loopback / 链路本地 / 云元数据 =====
_ALLOWED_SCHEMES = ("http", "https")
_DISALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("10.0.0.0/8"),        # private
    ipaddress.ip_network("172.16.0.0/12"),     # private
    ipaddress.ip_network("192.168.0.0/16"),    # private
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (云 metadata 169.254.169.254)
    ipaddress.ip_network("0.0.0.0/8"),         # wildcard
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _validate_safe_url(url: str) -> str:
    """校验 URL 不指向私网/loopback/metadata。返回原始 url 或抛 400。"""
    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="URL 格式无效")
    if p.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"不支持的协议: {p.scheme}")
    host = p.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL 缺少主机名")
    try:
        # 解析域名 → IP
        infos = socket.getaddrinfo(host, None)
        ips = {i[4][0] for i in infos}
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"无法解析域名: {host}")
    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        # IPv4-mapped IPv6 转 v4
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for net in _DISALLOWED_NETWORKS:
            if ip.version == net.version and ip in net:
                raise HTTPException(
                    status_code=400,
                    detail=f"URL 指向禁止的地址段 ({net})，已拦截",
                )
    return url

from ..dependencies import require_admin
from ..parser import (
    ParseError,
    detect_subscription_format,
    guess_country,
    parse_subscription,
    to_yaml,
)
from ..repository import (
    add_tag_to_proxies,
    create_external_sub,
    delete_external_sub,
    get_custom_proxy_by_name,
    get_external_sub,
    get_external_sub_by_name,
    get_tag,
    insert_custom_proxy,
    list_external_subs,
    update_external_sub,
    update_external_sub_sync_result,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/external-subs",
    tags=["external-subs"],
    dependencies=[Depends(require_admin)],
)


# ----- 输入模型 -----

class CreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    url: str = Field(..., min_length=8)
    user_agent: str | None = None
    fmt: str = "auto"                # auto / uri_list / clash_yaml
    enabled: bool = True
    sync_mode: str = Field(default="interval", pattern=r"^(interval|daily|weekly|off)$")
    sync_interval_min: int = Field(default=360, ge=0, le=10080)
    sync_time: str = Field(default="03:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    sync_weekday: int = Field(default=0, ge=0, le=6)
    default_tag_ids: list[int] = []
    name_filter_regex: str | None = None

    @field_validator("name_filter_regex")
    @classmethod
    def _check_regex(cls, v):
        if v is None or v == "":
            return None
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}")
        return v


class UpdateBody(BaseModel):
    name: str | None = None
    url: str | None = None
    user_agent: str | None = None
    fmt: str | None = None
    enabled: bool | None = None
    sync_mode: str | None = Field(default=None, pattern=r"^(interval|daily|weekly|off)$")
    sync_interval_min: int | None = Field(default=None, ge=0, le=10080)
    sync_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    sync_weekday: int | None = Field(default=None, ge=0, le=6)
    default_tag_ids: list[int] | None = None
    name_filter_regex: str | None = None

    @field_validator("name_filter_regex")
    @classmethod
    def _check_regex(cls, v):
        if v is None or v == "":
            return None
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}")
        return v


# ----- traffic header 解析 -----

def _parse_userinfo_header(header: str | None) -> dict[str, int | None]:
    """解析 subscription-userinfo: upload=..;download=..;total=..;expire=..
    返回 {upload, download, total, expire}（缺字段为 None）"""
    out: dict[str, int | None] = {"upload": None, "download": None, "total": None, "expire": None}
    if not header:
        return out
    for part in header.split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        try:
            num = int(v.strip())
        except ValueError:
            continue
        if k == "upload":      out["upload"] = num
        elif k == "download":  out["download"] = num
        elif k == "total":     out["total"] = num
        elif k == "expire":    out["expire"] = num
    return out


def _types_summary(nodes: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in nodes:
        out[n["type"]] = out.get(n["type"], 0) + 1
    return out


# ----- 同步逻辑（共用，可被 API 和 scheduler 调用） -----

async def sync_one(sub: dict[str, Any]) -> dict[str, Any]:
    """同步单个外部订阅。返回摘要 {added, skipped, total, traffic, ...}"""
    _validate_safe_url(sub["url"])            # SSRF 防护
    url = sub["url"]
    fmt = sub.get("fmt") or "auto"
    ua = sub.get("user_agent") or "clash-meta/1.19"
    tag_ids: list[int] = sub.get("default_tag_ids") or []
    name_filter: str | None = sub.get("name_filter_regex") or None
    headers = {"User-Agent": ua}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.text
            userinfo = _parse_userinfo_header(r.headers.get("subscription-userinfo"))
    except httpx.HTTPStatusError as e:
        err = f"HTTP {e.response.status_code}"
        update_external_sub_sync_result(sub["id"], ok=False, error=err, added_count=0, traffic={})
        raise HTTPException(status_code=502, detail=err)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        update_external_sub_sync_result(sub["id"], ok=False, error=err, added_count=0, traffic={})
        raise HTTPException(status_code=502, detail=f"抓取失败: {err}")

    detected = detect_subscription_format(text) if fmt == "auto" else fmt
    try:
        nodes = parse_subscription(text, fmt=fmt)
    except ParseError as e:
        update_external_sub_sync_result(
            sub["id"], ok=False, error=f"解析失败 ({detected}): {e}",
            added_count=0, traffic=userinfo,
        )
        raise HTTPException(status_code=400, detail=f"解析失败: {e} (detected={detected})")

    # 编译正则
    name_re = re.compile(name_filter) if name_filter else None

    added_ids, skipped, filtered_out = [], [], []
    for n in nodes:
        if name_re and name_re.search(n["name"]):
            filtered_out.append(n["name"])
            continue
        if get_custom_proxy_by_name(n["name"]):
            skipped.append({"name": n["name"], "reason": "已存在"})
            continue
        yaml_text = to_yaml(n)
        cid = insert_custom_proxy(
            name=n["name"], type_=n["type"], config_yaml=yaml_text,
            source_uri=None,
            group_name=sub["name"],
            country=guess_country(n["name"]),
            note=f"external_sub:{sub['id']}",
        )
        added_ids.append(cid)

    if tag_ids and added_ids:
        for tid in tag_ids:
            if get_tag(tid):
                add_tag_to_proxies(added_ids, [tid])

    update_external_sub_sync_result(
        sub["id"], ok=True, error=None,
        added_count=len(added_ids), traffic=userinfo,
    )
    return {
        "added": len(added_ids),
        "skipped": len(skipped),
        "filtered_out": len(filtered_out),
        "filtered_out_sample": filtered_out[:5],
        "total_parsed": len(nodes),
        "detected_format": detected,
        "traffic": userinfo,
    }


# ----- CRUD 路由 -----

@router.get("")
def list_all() -> list[dict[str, Any]]:
    return list_external_subs()


@router.post("")
def create(body: CreateBody) -> dict[str, Any]:
    if get_external_sub_by_name(body.name):
        raise HTTPException(status_code=409, detail=f"名称 {body.name!r} 已存在")
    sid = create_external_sub(
        name=body.name, url=body.url, user_agent=body.user_agent,
        fmt=body.fmt, enabled=body.enabled,
        sync_mode=body.sync_mode, sync_interval_min=body.sync_interval_min,
        sync_time=body.sync_time, sync_weekday=body.sync_weekday,
        default_tag_ids=body.default_tag_ids,
        name_filter_regex=body.name_filter_regex,
    )
    return {"id": sid, "name": body.name}


@router.patch("/{sid}")
def update(sid: int, body: UpdateBody) -> dict[str, str]:
    if not get_external_sub(sid):
        raise HTTPException(status_code=404, detail="不存在")
    update_external_sub(
        sid,
        name=body.name, url=body.url, user_agent=body.user_agent,
        fmt=body.fmt, enabled=body.enabled,
        sync_mode=body.sync_mode, sync_interval_min=body.sync_interval_min,
        sync_time=body.sync_time, sync_weekday=body.sync_weekday,
        default_tag_ids=body.default_tag_ids,
        name_filter_regex=body.name_filter_regex,
    )
    return {"status": "ok"}


@router.delete("/{sid}")
def delete(sid: int) -> dict[str, str]:
    delete_external_sub(sid)
    return {"status": "ok"}


@router.post("/{sid}/sync")
async def sync_now(sid: int) -> dict[str, Any]:
    sub = get_external_sub(sid)
    if not sub:
        raise HTTPException(status_code=404, detail="不存在")
    return await sync_one(sub)


@router.get("/preview")
async def preview_external_sub(
    url: str, fmt: str = "auto", user_agent: str | None = None
) -> dict[str, Any]:
    """抓取 + 解析，不入库也不写状态，返回节点摘要 + 流量信息。"""
    _validate_safe_url(url)                  # SSRF 防护
    headers = {"User-Agent": user_agent or "clash-meta/1.19"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.text
            userinfo = _parse_userinfo_header(r.headers.get("subscription-userinfo"))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取失败: {type(e).__name__}: {e}")

    detected = detect_subscription_format(text) if fmt == "auto" else fmt
    try:
        nodes = parse_subscription(text, fmt=fmt)
        parsed_ok = True
        parse_err = None
    except ParseError as e:
        nodes = []
        parsed_ok = False
        parse_err = str(e)

    # 精简版节点列表（去掉大字段如 password/reality-opts，给前端表格用）
    slim_nodes = [{
        "name": n["name"],
        "type": n["type"],
        "server": n.get("server"),
        "port": n.get("port"),
        "network": n.get("network"),
        "cipher": n.get("cipher"),
        "security": n.get("security"),
    } for n in nodes]

    return {
        "fetched_chars": len(text),
        "detected_format": detected,
        "parsed_ok": parsed_ok,
        "parse_error": parse_err,
        "total_parsed": len(nodes),
        "names_sample": [n["name"] for n in nodes[:20]],
        "types_summary": _types_summary(nodes),
        "traffic": userinfo,
        "raw_head": text[:300],
        "nodes": slim_nodes,                       # 完整节点精简列表
    }


@router.post("/{sid}/preview")
async def preview_saved(sid: int) -> dict[str, Any]:
    """预览已保存订阅的当前内容（不入库）。"""
    sub = get_external_sub(sid)
    if not sub:
        raise HTTPException(status_code=404, detail="不存在")
    return await preview_external_sub(sub["url"], sub.get("fmt") or "auto", sub.get("user_agent"))


@router.post("/sync-all")
async def sync_all() -> dict[str, Any]:
    subs = list_external_subs()
    results = []
    for s in subs:
        if not s.get("enabled"):
            continue
        try:
            res = await sync_one(s)
            results.append({"id": s["id"], "name": s["name"], "ok": True, **res})
        except HTTPException as e:
            results.append({"id": s["id"], "name": s["name"], "ok": False, "error": e.detail})
        except Exception as e:
            results.append({"id": s["id"], "name": s["name"], "ok": False, "error": str(e)})
    return {"count": len(results), "results": results}