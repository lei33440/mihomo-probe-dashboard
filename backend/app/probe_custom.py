"""TCP / TLS 连通性测试：不依赖 mihomo，直接测节点端口是否存活 + 粗延迟。

注意：这只验证「能不能连上 / TLS 握上手」，不是真正的代理延迟。
真正的代理延迟需要把节点加进 mihomo 后用 /proxies/:name/delay 测。
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Any

log = logging.getLogger(__name__)


async def _tcp_only(host: str, port: int, timeout: float) -> tuple[bool, float, str | None, str | None]:
    """纯 TCP 连接，返回 (ok, connect_ms, ip, error)"""
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return False, 0.0, None, f"tcp timeout after {timeout}s"
    except Exception as e:
        return False, 0.0, None, f"tcp {type(e).__name__}: {e}"
    connect_ms = (time.monotonic() - t0) * 1000
    peername = writer.get_extra_info("peername")
    ip = f"{peername[0]}:{peername[1]}" if peername else None
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True, connect_ms, ip, None


async def _tcp_with_tls(
    host: str, port: int, timeout: float, sni: str | None
) -> tuple[bool, float, str | None, str | None]:
    """TCP + TLS 一次完成，返回 (ok, total_ms, ip, error)"""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl_ctx.server_hostname = sni or host
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=sni or host),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return False, 0.0, None, f"tls timeout after {timeout}s"
    except Exception as e:
        return False, 0.0, None, f"tls {type(e).__name__}: {e}"
    total_ms = (time.monotonic() - t0) * 1000
    peername = writer.get_extra_info("peername")
    ip = f"{peername[0]}:{peername[1]}" if peername else None
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True, total_ms, ip, None


async def tcp_probe(
    host: str, port: int, *, timeout: float = 5.0,
    tls: bool = False, sni: str | None = None,
) -> dict[str, Any]:
    """返回 {ok, connect_ms, tls_ms, total_ms, ip, error}"""
    # 1) TCP-only：测裸连接耗时
    tcp_ok, tcp_ms, ip, tcp_err = await _tcp_only(host, port, timeout)
    if not tcp_ok:
        return {
            "ok": False, "error": tcp_err,
            "host": host, "port": port, "ip": ip,
            "connect_ms": None, "tls_ms": None, "total_ms": None,
        }

    if not tls:
        return {
            "ok": True,
            "host": host, "port": port, "ip": ip,
            "connect_ms": round(tcp_ms, 1),
            "tls_ms": None,
            "total_ms": round(tcp_ms, 1),
        }

    # 2) TLS：另起一次连接测握手 + 加密通道建立总耗时
    tls_ok, tls_total_ms, _, tls_err = await _tcp_with_tls(host, port, timeout, sni)
    if not tls_ok:
        return {
            "ok": False, "error": tls_err,
            "host": host, "port": port, "ip": ip,
            "connect_ms": round(tcp_ms, 1),
            "tls_ms": None, "total_ms": None,
        }

    return {
        "ok": True,
        "host": host, "port": port, "ip": ip,
        "connect_ms": round(tcp_ms, 1),
        "tls_ms": round(tls_total_ms - tcp_ms, 1),  # TLS 段耗时估算
        "total_ms": round(tls_total_ms, 1),
    }