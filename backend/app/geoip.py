"""GeoIP：根据 IP / 域名解析国家码。

策略（按优先级）：
1. 启发式匹配（节点名关键字，如"香港"/"Hongkong"/"JP" 等）— 零网络
3. 在线查询 ip-api.com（无需 key，45 req/min）— 缓存到内存 + DB

ip-api.com 返回字段：{countryCode: "HK", country: "Hong Kong", query: "1.2.3.4", ...}
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ISO 3166-1 alpha-2 → 国旗 emoji
def flag_for(code: Optional[str]) -> Optional[str]:
    if not code or len(code) != 2:
        return None
    a, b = code.upper()
    if not ('A' <= a <= 'Z' and 'A' <= b <= 'Z'):
        return None
    A = 0x1F1E6
    return chr(A + ord(a) - ord('A')) + chr(A + ord(b) - ord('A'))


# 启发式关键字
_HINTS = [
    ("中国", "CN"), ("CN", "CN"), ("China", "CN"), ("回国", "CN"),
    ("香港", "HK"), ("HK", "HK"), ("HongKong", "HK"), ("Hong Kong", "HK"), ("Hongkong", "HK"),
    ("台湾", "TW"), ("TW", "TW"), ("Taiwan", "TW"),
    ("日本", "JP"), ("JP", "JP"), ("Japan", "JP"),
    ("美国", "US"), ("US", "US"), ("USA", "US"), ("UnitedStates", "US"), ("United States", "US"),
    ("新加坡", "SG"), ("SG", "SG"), ("Singapore", "SG"),
    ("韩国", "KR"), ("KR", "KR"), ("Korea", "KR"),
    ("英国", "GB"), ("UK", "GB"), ("GB", "GB"), ("England", "GB"),
    ("德国", "DE"), ("DE", "DE"), ("Germany", "DE"),
    ("法国", "FR"), ("FR", "FR"), ("France", "FR"),
    ("加拿大", "CA"), ("CA", "CA"), ("Canada", "CA"),
    ("澳大利亚", "AU"), ("AU", "AU"), ("Australia", "AU"),
    ("俄罗斯", "RU"), ("RU", "RU"), ("Russia", "RU"),
    ("印度", "IN"), ("IN", "IN"), ("India", "IN"),
    ("巴西", "BR"), ("BR", "BR"), ("Brazil", "BR"),
    ("马来西亚", "MY"), ("MY", "MY"), ("Malaysia", "MY"),
    ("泰国", "TH"), ("TH", "TH"), ("Thailand", "TH"),
    ("越南", "VN"), ("VN", "VN"), ("Vietnam", "VN"),
    ("菲律宾", "PH"), ("PH", "PH"), ("Philippines", "PH"),
    ("印尼", "ID"), ("ID", "ID"), ("Indonesia", "ID"),
    ("土耳其", "TR"), ("TR", "TR"), ("Turkey", "TR"),
    ("阿联酋", "AE"), ("AE", "AE"), ("UAE", "AE"),
    ("荷兰", "NL"), ("NL", "NL"), ("Netherlands", "NL"),
    ("意大利", "IT"), ("IT", "IT"), ("Italy", "IT"),
    ("西班牙", "ES"), ("ES", "ES"), ("Spain", "ES"),
    ("阿根廷", "AR"), ("AR", "AR"), ("Argentina", "AR"),
    ("墨西哥", "MX"), ("MX", "MX"), ("Mexico", "MX"),
    ("南非", "ZA"), ("ZA", "ZA"), ("SouthAfrica", "ZA"), ("South Africa", "ZA"),
]


def guess_country(name: str) -> Optional[str]:
    if not name:
        return None
    for hint, code in _HINTS:
        if hint in name:
            return code
    return None


# ===== 在线查询 =====
# 缓存 IP → (country_code, expire_ts_ms)
_ip_cache: dict[str, tuple[str, int]] = {}
_CACHE_TTL_MS = 7 * 86400_000     # 7 天
_query_lock = asyncio.Lock()


_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _looks_like_ip(s: str) -> bool:
    return bool(s) and bool(_IPV4_RE.match(s))


def _resolve_domain(domain: str, timeout: float = 2.0) -> Optional[str]:
    """域名 → IPv4（短超时）。返回 None 表示失败。"""
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except Exception:
        return None


async def lookup_ip(country_ip_or_domain: str, *, use_cache: bool = True) -> Optional[str]:
    """返回 ISO 3166-1 alpha-2 country code（如 'HK'）。失败返回 None。"""
    if not country_ip_or_domain:
        return None

    # 1. 域名先解析成 IP
    target = country_ip_or_domain
    if not _looks_like_ip(target):
        ip = await asyncio.to_thread(_resolve_domain, target)
        if not ip:
            return None
        target = ip

    # 2. 缓存
    now_ms = int(__import__("time").time() * 1000)
    if use_cache:
        cached = _ip_cache.get(target)
        if cached and cached[1] > now_ms:
            return cached[0]

    # 3. 查 ip-api.com
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"http://ip-api.com/json/{target}",
                params={"fields": "status,countryCode,query"},
            )
            data = r.json()
    except Exception as e:
        log.debug("geoip lookup %s failed: %s", target, e)
        return None

    if data.get("status") != "success":
        return None
    code = (data.get("countryCode") or "").upper()
    if len(code) != 2:
        return None
    _ip_cache[target] = (code, now_ms + _CACHE_TTL_MS)
    return code


async def lookup_country(name: str, host: Optional[str] = None) -> Optional[str]:
    """组合启发式 + IP 查询。返回 country code 或 None。"""
    # 1. 启发式
    c = guess_country(name)
    if c:
        return c
    # 2. IP 查询
    if host:
        return await lookup_ip(host)
    return None