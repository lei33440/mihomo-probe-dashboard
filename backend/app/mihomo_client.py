"""Mihomo REST API 客户端。"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class MihomoError(Exception):
    pass


# ===== Demo 模式：本地没有 mihomo 时注入假数据 =====

_DEMO_NODES: list[dict[str, Any]] = [
    {"name": "🇭🇰 香港-HK-01", "type": "vmess",     "group": "香港",   "base": 85},
    {"name": "🇭🇰 香港-HK-02", "type": "trojan",    "group": "香港",   "base": 110},
    {"name": "🇹🇼 台湾-TW-01", "type": "vless",     "group": "台湾",   "base": 140},
    {"name": "🇯🇵 日本-JP-01", "type": "ss",        "group": "日本",   "base": 180},
    {"name": "🇯🇵 日本-JP-02", "type": "hysteria2", "group": "日本",   "base": 220},
    {"name": "🇸🇬 新加坡-SG-01", "type": "trojan",  "group": "新加坡", "base": 165},
    {"name": "🇺🇸 美国-US-01", "type": "vless",     "group": "美国",   "base": 280},
    {"name": "🇺🇸 美国-US-02", "type": "vmess",     "group": "美国",   "base": 310},
    {"name": "🇰🇷 韩国-KR-01", "type": "ss",        "group": "其它地区", "base": 200},
    {"name": "🇬🇧 英国-GB-01", "type": "vless",     "group": "其它地区", "base": 360},
    {"name": "🇩🇪 德国-DE-01", "type": "trojan",    "group": "其它地区", "base": 340},
    {"name": "🇨🇳 直连",     "type": "direct",     "group": "直连",   "base": 25},
]


def _demo_seed(name: str) -> int:
    # 每分钟种子变一次 → 曲线有"上下波动"
    return hash((name, int(time.time()) // 60)) & 0xFFFFFFFF


def _demo_probe_one(name: str) -> dict[str, Any]:
    rng = random.Random(_demo_seed(name))
    base = next((n["base"] for n in _DEMO_NODES if n["name"] == name), 150)
    # 90% 成功率；失败时随机选个错误
    if rng.random() < 0.08:
        return {
            "name": name,
            "success": False,
            "delay": None,
            "error": rng.choice([
                "i/o timeout",
                "connection refused",
                "no route to host",
                "TLS handshake timeout",
            ]),
        }
    # 延迟在 base 的 0.6 ~ 1.5 之间波动，偶发 spike 到 3x
    factor = 0.6 + rng.random() * 0.9
    if rng.random() < 0.05:
        factor *= 2
    delay = max(8, int(base * factor))
    return {"name": name, "success": True, "delay": delay, "error": None}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.mihomo_secret}"} if settings.mihomo_secret else {}


async def _get(path: str, client: httpx.AsyncClient) -> dict[str, Any]:
    url = f"{settings.mihomo_api_url.rstrip('/')}{path}"
    r = await client.get(url, headers=_headers(), timeout=10)
    if r.status_code != 200:
        raise MihomoError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


async def fetch_proxies(client: httpx.AsyncClient) -> dict[str, Any]:
    """返回 /proxies 完整结构，含 all / proxies / providers / 组、节点等。"""
    return await _get("/proxies", client)


async def probe_one(name: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """调用 /proxies/:name/delay，返回 {delay, meanDelay} 或抛 MihomoError。"""
    from urllib.parse import quote

    path = (
        f"/proxies/{quote(name, safe='')}/delay"
        f"?url={quote(settings.probe_url, safe='')}"
        f"&timeout={settings.probe_timeout}"
    )
    return await _get(path, client)


async def list_node_names() -> list[dict[str, str]]:
    """返回 [{name, type, group}]，跳过 group / URLTester / 自身。"""
    if settings.demo_mode:
        return [{"name": n["name"], "type": n["type"], "group": n["group"]} for n in _DEMO_NODES]

    async with httpx.AsyncClient() as client:
        try:
            data = await fetch_proxies(client)
        except Exception as e:
            log.error("fetch /proxies failed: %s", e)
            return []

    nodes: list[dict[str, str]] = []
    proxies_section = data.get("proxies", {}) or {}

    # 找到所有非 selector/url-test/fallback/load-balance 的项
    skip_group_types = {"Selector", "URLTest", "Fallback", "LoadBalance"}

    for name, info in proxies_section.items():
        if not isinstance(info, dict):
            continue
        if info.get("type") in skip_group_types:
            continue
        # 类型是 ss/vmess/vless/trojan/hysteria2/snell/direct/socks/http/...
        nodes.append({
            "name": name,
            "type": info.get("type") or "",
        })

    # 从 proxies 顶层推断 group 归属
    groups_of: dict[str, str | None] = {n["name"]: None for n in nodes}
    for gname, ginfo in proxies_section.items():
        if not isinstance(ginfo, dict) or ginfo.get("type") not in skip_group_types:
            continue
        for member in (ginfo.get("all") or []):
            if member in groups_of and groups_of[member] is None:
                groups_of[member] = gname

    for n in nodes:
        n["group"] = groups_of.get(n["name"])

    return nodes


async def probe_many(names: list[str]) -> list[dict[str, Any]]:
    """并发探测，返回 [{name, success, delay, error}]。"""
    if settings.demo_mode:
        # demo 模式：模拟每个节点的探测结果
        await asyncio.sleep(0.05)  # 让前端感觉"探测需要时间"
        return [_demo_probe_one(n) for n in names]

    sem = asyncio.Semaphore(settings.probe_concurrency)

    async with httpx.AsyncClient() as client:

        async def one(name: str) -> dict[str, Any]:
            async with sem:
                try:
                    data = await probe_one(name, client)
                    delay = data.get("delay", 0)
                    if delay < 0 or delay >= settings.probe_timeout:
                        return {
                            "name": name,
                            "success": False,
                            "delay": None,
                            "error": f"timeout or invalid delay={delay}",
                        }
                    return {
                        "name": name,
                        "success": True,
                        "delay": int(delay),
                        "error": None,
                    }
                except MihomoError as e:
                    return {
                        "name": name,
                        "success": False,
                        "delay": None,
                        "error": str(e),
                    }
                except Exception as e:
                    return {
                        "name": name,
                        "success": False,
                        "delay": None,
                        "error": f"{type(e).__name__}: {e}",
                    }

        return await asyncio.gather(*(one(n) for n in names))