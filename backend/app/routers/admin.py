"""管理员 API：完整数据 + 历史曲线 + 手动触发。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import require_admin
from ..repository import (
    bulk_get_proxy_tags,
    fetch_aggregates,
    fetch_probes_in_range,
    get_proxy_by_name,
    list_proxies,
    public_view,
)
from ..scheduler import run_probe_cycle
from ..mihomo_client import list_node_names, probe_many

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/proxies")
def admin_proxies() -> list[dict]:
    items = public_view()
    by_id = {p["id"]: p for p in list_proxies()}
    by_name = {p["name"]: p for p in list_proxies()}
    tag_map = bulk_get_proxy_tags()  # {name: [tags]}
    # 对 custom 来源节点解析 yaml 拿 server/port/network/cipher
    import yaml as _yaml
    from ..repository import get_custom_proxy_by_name
    for it in items:
        full = by_id.get(it["id"]) or by_name.get(it["name"], {})
        it["source"] = full.get("source", "mihomo")
        it["created_at"] = full.get("created_at")
        it["updated_at"] = full.get("updated_at")
        it["tags"] = tag_map.get(it["name"], [])
        # 默认值
        it["server"] = None
        it["port"] = None
        it["network"] = None
        it["cipher"] = None
        if it["source"] == "custom":
            cp = get_custom_proxy_by_name(it["name"])
            if cp:
                try:
                    y = _yaml.safe_load(cp["config_yaml"])
                    if isinstance(y, dict):
                        it["server"] = y.get("server")
                        it["port"] = y.get("port")
                        it["network"] = y.get("network")
                        it["cipher"] = y.get("cipher") or y.get("security")
                except Exception:
                    pass
    return items


@router.get("/proxies/{name}/history")
def admin_history(name: str, range: str = "24h") -> dict:
    """返回原始点 + 5min 聚合 + 1h 聚合，按 range 选粒度。"""
    p = get_proxy_by_name(name)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    pid = p["id"]
    now_ms = int(time.time() * 1000)
    span_ms = _parse_range(range)
    start_ms = now_ms - span_ms

    # range 决定精度
    if span_ms <= 6 * 3600_000:  # <=6h 用原始点
        raw = fetch_probes_in_range(pid, start_ms, now_ms, limit=20000)
        fivemin = []
        oneh = []
        bucket = "raw"
    elif span_ms <= 7 * 86400_000:  # <=7d 用 5min
        raw = []
        fivemin = fetch_aggregates("aggregates_5min", pid, start_ms, now_ms)
        oneh = []
        bucket = "5min"
    else:  # >7d 用 1h
        raw = []
        fivemin = []
        oneh = fetch_aggregates("aggregates_1h", pid, start_ms, now_ms)
        bucket = "1h"

    return {
        "name": name,
        "range": range,
        "bucket": bucket,
        "start_ts": start_ms,
        "end_ts": now_ms,
        "raw": raw,
        "5min": fivemin,
        "1h": oneh,
    }


def _parse_range(r: str) -> int:
    r = r.lower()
    if r.endswith("h"):
        return int(r[:-1]) * 3600_000
    if r.endswith("d"):
        return int(r[:-1]) * 86400_000
    if r.endswith("m"):
        return int(r[:-1]) * 60_000
    return 24 * 3600_000


@router.post("/proxies/{name}/probe")
async def admin_probe_one(name: str) -> dict:
    p = get_proxy_by_name(name)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    results = await probe_many([name])
    if not results:
        raise HTTPException(status_code=502, detail="mihomo unreachable")
    r = results[0]
    now_ms = int(time.time() * 1000)
    from ..repository import insert_probe
    insert_probe(p["id"], now_ms, r["success"], r["delay"], r["error"])
    return {
        "name": name,
        "success": r["success"],
        "delay": r["delay"],
        "error": r["error"],
        "ts": now_ms,
    }


@router.post("/probes/all")
async def admin_probe_all() -> dict:
    res = await run_probe_cycle()
    return res


@router.post("/sync-nodes")
async def admin_sync_nodes() -> dict:
    """只同步节点列表不探测。"""
    nodes = await list_node_names()
    from ..repository import upsert_proxy
    from ..scheduler import _guess_country
    for n in nodes:
        upsert_proxy(
            name=n["name"],
            type_=n["type"] or None,
            provider=None,
            group_name=n.get("group"),
            country=_guess_country(n["name"]),
        )
    return {"count": len(nodes)}