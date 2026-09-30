"""APScheduler 定时任务。"""
from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import aggregator, mihomo_client, repository as repo
from .config import settings
from .geoip import flag_for, guess_country, lookup_ip
from .probe_custom import tcp_probe

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=settings.tz)


@scheduler.scheduled_job(IntervalTrigger(seconds=settings.probe_interval), id="probe_all")
async def _probe_all_job() -> None:
    await run_probe_cycle()


async def run_probe_cycle() -> dict[str, int]:
    """一轮全量探测。返回 {ok, fail, total}。"""
    t0 = time.monotonic()
    nodes = await mihomo_client.list_node_names()
    custom_proxies = repo.list_custom_proxies(enabled_only=True)
    if not nodes and not custom_proxies:
        log.warning("no nodes discovered from mihomo or custom_proxies")
        return {"ok": 0, "fail": 0, "total": 0}

    # 同步 mihomo 节点到 DB（启发式补 country）
    for n in nodes:
        repo.upsert_proxy(
            name=n["name"],
            type_=n["type"] or None,
            provider=None,
            group_name=n.get("group"),
            country=guess_country(n["name"]),
        )

    db_proxies = {p["name"]: p for p in repo.list_proxies()}
    target_mihomo = [n["name"] for n in nodes]

    # mihomo 节点并发探测
    results: list[dict] = []
    if target_mihomo:
        results.extend(await mihomo_client.probe_many(target_mihomo))

    # custom 节点：直接全部探测（不依赖 proxies 表）
    if custom_proxies:
        import yaml as _yaml
        custom_results = []
        ip_lookup_tasks: list[tuple[str, str]] = []
        probe_payloads: list[dict] = []

        for p in custom_proxies:
            full = repo.get_custom_proxy(p["id"])
            if not full:
                continue
            try:
                node = _yaml.safe_load(full["config_yaml"])
            except Exception:
                continue
            host = node.get("server")
            port = node.get("port")
            if not (host and port):
                continue
            tls = bool(node.get("security") in ("reality", "tls") or node.get("tls"))
            sni = None
            if isinstance(node.get("reality-opts"), dict):
                sni = node["reality-opts"].get("server-name")
            if not sni:
                sni = node.get("sni")
            # 确保 probes 表有 FK 目标：在 proxies 表里给 custom 节点建一行
            pid = db_proxies.get(p["name"], {}).get("id")
            if pid is None:
                # 用 upsert_proxy 把 custom 节点同步进 proxies 表（country 启发式推断）
                repo.upsert_proxy(
                    name=p["name"], type_=p.get("type") or None,
                    provider=None, group_name=p.get("group_name"),
                    country=guess_country(p["name"]),
                )
                pid = repo.get_proxy_by_name(p["name"])["id"]
                db_proxies[p["name"]] = {"id": pid}
            probe_payloads.append({
                "name": p["name"], "host": host, "port": int(port),
                "tls": tls, "sni": sni, "pid": pid,
                "country_existing": full.get("country"),
            })
            # 启发式未识别 country → 用 IP 查询补
            if not full.get("country"):
                ip_lookup_tasks.append((p["name"], host))

        async def probe_one(pay):
            return await tcp_probe(pay["host"], pay["port"],
                                  timeout=settings.probe_timeout / 1000,
                                  tls=pay["tls"], sni=pay["sni"])

        if probe_payloads:
            probe_results = await asyncio.gather(*(probe_one(pay) for pay in probe_payloads))
        else:
            probe_results = []
        if ip_lookup_tasks:
            ip_results = await asyncio.gather(*(lookup_ip(host) for _, host in ip_lookup_tasks))
        else:
            ip_results = []

        ip_lookup_map = {}
        for (name, host), code in zip(ip_lookup_tasks, ip_results):
            if code:
                ip_lookup_map[name] = code
                log.info("GeoIP: %s -> %s", name, code)

        for pay, res in zip(probe_payloads, probe_results):
            success = bool(res.get("ok"))
            custom_results.append({
                "name": pay["name"],
                "success": success,
                "delay": int(res["total_ms"]) if success else None,
                "error": None if success else res.get("error"),
                "pid": pay["pid"],
            })
            new_country = ip_lookup_map.get(pay["name"])
            if new_country and not pay["country_existing"]:
                repo.update_proxy_country(pay["pid"], new_country)

        results.extend(custom_results)

    now_ms = int(time.time() * 1000)
    ok = fail = 0
    for r in results:
        pid = r.get("pid") or db_proxies.get(r["name"], {}).get("id")
        if pid is None:
            continue
        repo.insert_probe(pid, now_ms, r["success"], r["delay"], r["error"])
        if r["success"]:
            ok += 1
        else:
            fail += 1

    log.info(
        "probe cycle done: total=%d ok=%d fail=%d elapsed=%.2fs",
        len(results), ok, fail, time.monotonic() - t0,
    )
    return {"ok": ok, "fail": fail, "total": len(results)}


@scheduler.scheduled_job(IntervalTrigger(minutes=5), id="aggregate")
def _aggregate_job() -> None:
    try:
        aggregator.run_aggregations()
    except Exception:
        log.exception("aggregate job failed")


def _should_sync_now(sub: dict, now_ms: int) -> bool:
    """判断订阅是否到期。"""
    mode = sub.get("sync_mode", "interval")
    if mode == "off":
        return False
    last = sub.get("last_sync_ts") or 0
    if mode == "interval":
        interval_ms = (sub.get("sync_interval_min") or 0) * 60_000
        if interval_ms <= 0:
            return False
        # 第一次没同步过，立即同步
        if last == 0:
            return True
        return now_ms - last >= interval_ms
    if mode == "daily":
        try:
            hh, mm = (sub.get("sync_time") or "03:00").split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            hh, mm = 3, 0
        today_run = _today_at_ms(now_ms, hh, mm)
        # 还没到今天的执行时间，则下次 = 今天；否则下次 = 明天
        next_run = today_run if now_ms < today_run else today_run + 86400_000
        # last_sync_ts < next_run 表示还没执行过今天这次
        return last < next_run
    if mode == "weekly":
        try:
            hh, mm = (sub.get("sync_time") or "03:00").split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            hh, mm = 3, 0
        wd = int(sub.get("sync_weekday", 0))
        next_run = _next_weekday_ms(now_ms, wd, hh, mm)
        return last < next_run
    return False


def _today_at_ms(now_ms: int, hh: int, mm: int) -> int:
    import datetime
    now = datetime.datetime.fromtimestamp(now_ms / 1000)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return int(target.timestamp() * 1000)


def _next_weekday_ms(now_ms: int, weekday: int, hh: int, mm: int) -> int:
    """下一个指定 weekday 的 HH:MM 的 unix ms。weekday: 0=周一 .. 6=周日"""
    import datetime
    now = datetime.datetime.fromtimestamp(now_ms / 1000)
    today_wd = now.weekday()
    days_ahead = (weekday - today_wd) % 7
    target = (now + datetime.timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if days_ahead == 0 and target.timestamp() * 1000 < now_ms:
        target = (now + datetime.timedelta(days=7)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return int(target.timestamp() * 1000)


@scheduler.scheduled_job(IntervalTrigger(seconds=60), id="external_sub_sync_check")
async def _external_sub_sync_job() -> None:
    """每 60s 检查所有启用的外部订阅，看是否到期需要同步。"""
    try:
        now_ms = int(time.time() * 1000)
        subs = repo.list_enabled_external_subs()
        for s in subs:
            if not _should_sync_now(s, now_ms):
                continue
            try:
                from .routers.external_subs import sync_one
                res = await sync_one(s)
                log.info("external sub '%s' synced: added=%d skipped=%d total=%d",
                         s["name"], res["added"], res["skipped"], res["total_parsed"])
            except Exception as e:
                log.exception("external sub '%s' sync failed", s["name"])
    except Exception:
        log.exception("external sub sync job failed")


# 简单的国家关键字映射
_COUNTRY_HINTS = [
    ("香港", "HK"), ("HK", "HK"), ("Hong Kong", "HK"), ("HongKong", "HK"),
    ("台湾", "TW"), ("TW", "TW"), ("Taiwan", "TW"),
    ("日本", "JP"), ("JP", "JP"), ("Japan", "JP"),
    ("美国", "US"), ("US", "US"), ("USA", "US"), ("United States", "US"),
    ("新加坡", "SG"), ("SG", "SG"), ("Singapore", "SG"),
    ("韩国", "KR"), ("KR", "KR"), ("Korea", "KR"),
    ("英国", "GB"), ("UK", "GB"), ("GB", "GB"),
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
    ("回国", "CN"), ("CN", "CN"), ("中国", "CN"), ("China", "CN"),
]


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        log.info("scheduler started: probe every %ds, aggregate every 5min",
                 settings.probe_interval)
        # 启动后立即触发一次探测，避免首屏空白等一个周期
        scheduler.add_job(
            run_probe_cycle,
            id="probe_all_immediate",
            replace_existing=True,
            next_run_time=__import__("datetime").datetime.now(),
        )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)