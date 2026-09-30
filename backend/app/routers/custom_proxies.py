"""custom_proxies CRUD 路由。"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import require_admin
from ..parser import (
    ParseError,
    detect_subscription_format,
    guess_country,
    parse_subscription,
    parse_uri,
    parse_yaml,
    to_yaml,
)
from ..probe_custom import tcp_probe
from ..repository import (
    add_tag_to_proxies,
    delete_custom_proxy,
    get_custom_proxy,
    get_custom_proxy_by_name,
    get_tag,
    insert_custom_proxy,
    list_custom_proxies,
    render_custom_proxies_yaml,
    set_proxy_tags,
    update_custom_proxy,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/custom-proxies",
    tags=["custom-proxies"],
    dependencies=[Depends(require_admin)],
)


# ----- 输入模型 -----

class AddUriBody(BaseModel):
    uri: str = Field(..., min_length=8)
    group_name: str | None = None
    note: str | None = None


class AddYamlBody(BaseModel):
    yaml_text: str = Field(..., min_length=4)
    group_name: str | None = None


class UpdateBody(BaseModel):
    name: str | None = None
    group_name: str | None = None
    enabled: bool | None = None
    note: str | None = None


class SubscribeBody(BaseModel):
    url: str = Field(..., min_length=8)
    fmt: str = "auto"               # auto / uri_list / clash_yaml
    group_name: str | None = None
    note: str | None = None
    user_agent: str | None = None
    tag_ids: list[int] = []          # 给本次导入的节点自动打这些标签


# ----- CRUD -----

@router.get("")
def list_all() -> list[dict[str, Any]]:
    return list_custom_proxies()


@router.post("")
def add_one(body: AddUriBody) -> dict[str, Any]:
    try:
        node = parse_uri(body.uri)
    except ParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = node["name"]
    if get_custom_proxy_by_name(name):
        raise HTTPException(status_code=409, detail=f"节点名 {name!r} 已存在")
    yaml_text = to_yaml(node)
    cid = insert_custom_proxy(
        name=name,
        type_=node["type"],
        config_yaml=yaml_text,
        source_uri=body.uri,
        group_name=body.group_name,
        country=guess_country(name),
        note=body.note,
    )
    return {"id": cid, "name": name, "type": node["type"]}


@router.post("/yaml")
def add_yaml(body: AddYamlBody) -> list[dict[str, Any]]:
    try:
        nodes = parse_yaml(body.yaml_text)
    except ParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    out = []
    for n in nodes:
        if get_custom_proxy_by_name(n["name"]):
            continue
        yaml_text = to_yaml(n)
        cid = insert_custom_proxy(
            name=n["name"],
            type_=n["type"],
            config_yaml=yaml_text,
            source_uri=None,
            group_name=body.group_name,
            country=guess_country(n["name"]),
            note=None,
        )
        out.append({"id": cid, "name": n["name"], "type": n["type"]})
    if not out:
        raise HTTPException(status_code=409, detail="yaml 里所有节点名都和已有的重复")
    return out


@router.post("/subscribe")
async def add_from_subscribe(body: SubscribeBody) -> dict[str, Any]:
    """抓取远程订阅 URL，解析后批量入库。"""
    # 1) 抓取
    headers = {"User-Agent": body.user_agent or "clash-meta/1.19"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            r = await client.get(body.url, headers=headers)
            r.raise_for_status()
            text = r.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"订阅 HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取失败: {type(e).__name__}: {e}")

    # 2) 检测/解析
    fmt = body.fmt if body.fmt in ("auto", "uri_list", "clash_yaml") else "auto"
    detected = detect_subscription_format(text) if fmt == "auto" else fmt
    try:
        nodes = parse_subscription(text, fmt=fmt)
    except ParseError as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e} (detected={detected})")

    # 3) 入库（跳过同名）
    added, skipped = [], []
    for n in nodes:
        if get_custom_proxy_by_name(n["name"]):
            skipped.append({"name": n["name"], "reason": "已存在"})
            continue
        yaml_text = to_yaml(n)
        cid = insert_custom_proxy(
            name=n["name"],
            type_=n["type"],
            config_yaml=yaml_text,
            source_uri=None,
            group_name=body.group_name,
            country=guess_country(n["name"]),
            note=body.note,
        )
        added.append({"id": cid, "name": n["name"], "type": n["type"]})

    # 4) 给新增节点打标签
    if body.tag_ids and added:
        for tid in body.tag_ids:
            if get_tag(tid):
                add_tag_to_proxies([a["id"] for a in added], [tid])

    return {
        "fetched_chars": len(text),
        "detected_format": detected,
        "total_parsed": len(nodes),
        "added_count": len(added),
        "skipped_count": len(skipped),
        "added": added[:50],          # 不爆 payload
        "skipped": skipped[:50],
        "tagged_with": body.tag_ids,
    }


@router.get("/subscribe/preview")
async def preview_subscribe(
    url: str, fmt: str = "auto", user_agent: str | None = None
) -> dict[str, Any]:
    """只抓不存，返回摘要供 UI 预览。即使解析失败也回原始内容头部，方便排查。"""
    headers = {"User-Agent": user_agent or "clash-meta/1.19"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.text
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

    return {
        "fetched_chars": len(text),
        "detected_format": detected,
        "total_parsed": len(nodes),
        "parsed_ok": parsed_ok,
        "parse_error": parse_err,
        "names_sample": [n["name"] for n in nodes[:20]] if nodes else [],
        "types_summary": _types_summary(nodes) if nodes else {},
        "raw_head": text[:300],
        "raw_first_line": text.splitlines()[0][:300] if text else "",
        "line_count": len(text.splitlines()),
        "nodes": [{
            "name": n["name"], "type": n["type"],
            "server": n.get("server"), "port": n.get("port"),
            "network": n.get("network"),
            "cipher": n.get("cipher"), "security": n.get("security"),
        } for n in nodes] if nodes else [],
    }


def _types_summary(nodes: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in nodes:
        out[n["type"]] = out.get(n["type"], 0) + 1
    return out


@router.get("/{cid}")
def get_one(cid: int) -> dict[str, Any]:
    p = get_custom_proxy(cid)
    if not p:
        raise HTTPException(status_code=404, detail="not found")
    return p


@router.patch("/{cid}")
def update(cid: int, body: UpdateBody) -> dict[str, str]:
    p = get_custom_proxy(cid)
    if not p:
        raise HTTPException(status_code=404, detail="not found")
    update_custom_proxy(
        cid,
        name=body.name,
        group_name=body.group_name,
        enabled=1 if body.enabled else 0 if body.enabled is not None else None,
        note=body.note,
    )
    return {"status": "ok"}


@router.delete("/{cid}")
def delete(cid: int) -> dict[str, str]:
    delete_custom_proxy(cid)
    return {"status": "ok"}


@router.get("/export/yaml")
def export_yaml() -> dict[str, str]:
    """返回 yaml 字符串。供前端下载或 mihomo proxy-provider 直接消费。"""
    return {"yaml": render_custom_proxies_yaml()}


# ----- 连通性测试 -----

@router.post("/{cid}/test")
async def test_one(cid: int) -> dict[str, Any]:
    p = get_custom_proxy(cid)
    if not p:
        raise HTTPException(status_code=404, detail="not found")

    # 重新解析 yaml 取 server/port/tls 设置
    import yaml as _yaml
    try:
        node = _yaml.safe_load(p["config_yaml"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"yaml 解析失败: {e}")
    host = node.get("server")
    port = node.get("port")
    if not (host and port):
        raise HTTPException(status_code=400, detail="节点缺 server/port")

    tls = False
    sni = None
    if node.get("security") in ("reality", "tls") or node.get("tls"):
        tls = True
        # reality-opts.server-name 或 sni
        if isinstance(node.get("reality-opts"), dict):
            sni = node["reality-opts"].get("server-name")
        if not sni:
            sni = node.get("sni")

    res = await tcp_probe(host, int(port), timeout=8.0, tls=tls, sni=sni)
    res["node_name"] = p["name"]
    res["tls_requested"] = tls
    return res