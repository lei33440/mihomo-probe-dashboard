"""数据访问层：节点 / 探测 / 聚合 / 管理员 CRUD。"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from .database import db_cursor


# ====== proxies ======

def upsert_proxy(
    name: str,
    type_: str | None,
    provider: str | None,
    group_name: str | None,
    country: str | None,
) -> int:
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM proxies WHERE name = ?", (name,)
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE proxies SET type=?, provider=?, group_name=?, country=?, updated_at=? WHERE id=?",
                (type_, provider, group_name, country, now, row["id"]),
            )
            return row["id"]
        cur.execute(
            "INSERT INTO proxies (name, type, provider, group_name, country, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, type_, provider, group_name, country, now, now),
        )
        return cur.lastrowid


def list_proxies() -> list[dict[str, Any]]:
    """合并 mihomo 探测的 proxies + 用户管理的 custom_proxies（标记 source='custom'）。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, name, type, provider, group_name, country, created_at, updated_at "
            "FROM proxies ORDER BY group_name, name"
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["source"] = "mihomo"

        # 合并 custom_proxies（未在 proxies 表里出现的，避免重复）
        existing_names = {r["name"] for r in rows}
        cur.execute(
            "SELECT id, name, type, group_name, country, created_at, updated_at, enabled "
            "FROM custom_proxies WHERE enabled=1 ORDER BY group_name, name"
        )
        for r in cur.fetchall():
            r = dict(r)
            if r["name"] in existing_names:
                continue
            r["source"] = "custom"
            r["provider"] = None
            rows.append(r)
        return rows


def update_proxy_country(proxy_id: int, country: str) -> None:
    """更新 proxies.country（GeoIP 查询结果写回）。"""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE proxies SET country=?, updated_at=? WHERE id=?",
            (country, int(__import__("time").time() * 1000), proxy_id),
        )


def get_proxy_by_name(name: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM proxies WHERE name = ?", (name,))
        row = cur.fetchone()
        return dict(row) if row else None


# ====== probes ======

def insert_probe(proxy_id: int, ts: int, success: bool, delay_ms: int | None, error: str | None) -> None:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO probes (proxy_id, ts, success, delay_ms, error) VALUES (?, ?, ?, ?, ?)",
            (proxy_id, ts, 1 if success else 0, delay_ms, error),
        )


def latest_probe(proxy_id: int) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT ts, success, delay_ms, error FROM probes WHERE proxy_id=? ORDER BY ts DESC LIMIT 1",
            (proxy_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def bulk_latest_probes() -> dict[int, dict[str, Any]]:
    """返回 {proxy_id: 最新探测}。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT p.proxy_id, p.ts, p.success, p.delay_ms, p.error "
            "FROM probes p "
            "JOIN (SELECT proxy_id, MAX(ts) AS mts FROM probes GROUP BY proxy_id) m "
            "ON p.proxy_id = m.proxy_id AND p.ts = m.mts"
        )
        return {r["proxy_id"]: dict(r) for r in cur.fetchall()}


def fetch_probes_in_range(
    proxy_id: int, start_ts: int, end_ts: int, limit: int = 5000
) -> list[dict[str, Any]]:
    """获取原始探测点。"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT ts, success, delay_ms FROM probes "
            "WHERE proxy_id=? AND ts BETWEEN ? AND ? "
            "ORDER BY ts ASC LIMIT ?",
            (proxy_id, start_ts, end_ts, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_aggregates(
    table: str, proxy_id: int, start_ts: int, end_ts: int
) -> list[dict[str, Any]]:
    if table not in ("aggregates_5min", "aggregates_1h"):
        raise ValueError("invalid table")
    with db_cursor() as cur:
        cur.execute(
            f"SELECT bucket_ts, avg_delay, min_delay, max_delay, success_count, total_count "
            f"FROM {table} WHERE proxy_id=? AND bucket_ts BETWEEN ? AND ? ORDER BY bucket_ts ASC",
            (proxy_id, start_ts, end_ts),
        )
        return [dict(r) for r in cur.fetchall()]


# ====== 聚合写入 ======

def aggregate_into(table: str, bucket_seconds: int) -> int:
    """把已经存在的 probes / 5min 聚合 roll up 到 table。
    - table='aggregates_5min' 时源数据为 probes，桶容量 300s
    - table='aggregates_1h'  时源数据为 aggregates_5min，桶容量 3600s
    返回写入/更新的桶数。
    """
    if table == "aggregates_5min":
        bucket_ms = 300_000
        cur_sql = (
            "SELECT proxy_id, "
            "(ts/?)*? AS bucket_ts, "
            "AVG(delay_ms) AS avg_delay, "
            "MIN(delay_ms) AS min_delay, "
            "MAX(delay_ms) AS max_delay, "
            "SUM(success) AS success_count, "
            "COUNT(*) AS total_count "
            "FROM probes "
            "GROUP BY proxy_id, bucket_ts "
        )
        where_check = (
            f"SELECT 1 FROM {table} WHERE proxy_id=? AND bucket_ts=?"
        )
    elif table == "aggregates_1h":
        bucket_ms = 3_600_000
        cur_sql = (
            "SELECT proxy_id, "
            "(bucket_ts/?)*? AS bucket_ts, "
            "AVG(avg_delay) AS avg_delay, "
            "MIN(min_delay) AS min_delay, "
            "MAX(max_delay) AS max_delay, "
            "SUM(success_count) AS success_count, "
            "SUM(total_count) AS total_count "
            "FROM aggregates_5min "
            "GROUP BY proxy_id, bucket_ts "
        )
        where_check = (
            f"SELECT 1 FROM {table} WHERE proxy_id=? AND bucket_ts=?"
        )
    else:
        raise ValueError("invalid aggregate table")

    now_ms = int(time.time() * 1000)
    inserted = 0
    with db_cursor() as cur:
        cur.execute(cur_sql, (bucket_ms, bucket_ms))
        rows = cur.fetchall()
        for r in rows:
            proxy_id = r["proxy_id"]
            bucket_ts = r["bucket_ts"]
            if bucket_ts >= now_ms:
                continue
            cur.execute(where_check, (proxy_id, bucket_ts))
            if cur.fetchone():
                continue
            cur.execute(
                f"INSERT OR IGNORE INTO {table} "
                "(proxy_id, bucket_ts, avg_delay, min_delay, max_delay, success_count, total_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    proxy_id,
                    bucket_ts,
                    r["avg_delay"],
                    r["min_delay"],
                    r["max_delay"],
                    r["success_count"],
                    r["total_count"],
                ),
            )
            inserted += 1
    return inserted


def prune_old(table: str, older_than_ts: int) -> int:
    if table not in ("probes", "aggregates_5min", "aggregates_1h"):
        raise ValueError("invalid table")
    with db_cursor() as cur:
        if table == "probes":
            cur.execute("DELETE FROM probes WHERE ts < ?", (older_than_ts,))
        else:
            cur.execute(
                f"DELETE FROM {table} WHERE bucket_ts < ?", (older_than_ts,)
            )
        return cur.rowcount


# ====== custom_proxies ======

def insert_custom_proxy(
    name: str, type_: str, config_yaml: str,
    source_uri: str | None, group_name: str | None,
    country: str | None, note: str | None,
) -> int:
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO custom_proxies "
            "(name, type, config_yaml, source_uri, group_name, country, enabled, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (name, type_, config_yaml, source_uri, group_name, country, note, now, now),
        )
        return cur.lastrowid


def list_custom_proxies(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, name, type, group_name, country, enabled, note, "
        "created_at, updated_at, "
        "length(config_yaml) AS yaml_size, "
        "length(source_uri) AS uri_size "
        "FROM custom_proxies "
    )
    if enabled_only:
        sql += "WHERE enabled=1 "
    sql += "ORDER BY group_name, name"
    with db_cursor() as cur:
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]

    # 把 tags 拼到每行（按 name 关联）
    tag_map = bulk_get_proxy_tags()
    for r in rows:
        r["tags"] = tag_map.get(r["name"], [])
    return rows


def get_custom_proxy(id_: int) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM custom_proxies WHERE id=?", (id_,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_custom_proxy_by_name(name: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM custom_proxies WHERE name=?", (name,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_custom_proxy(
    id_: int, *, name: str | None = None, group_name: str | None = None,
    enabled: int | None = None, note: str | None = None,
) -> None:
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if group_name is not None:
        sets.append("group_name=?"); vals.append(group_name)
    if enabled is not None:
        sets.append("enabled=?"); vals.append(1 if enabled else 0)
    if note is not None:
        sets.append("note=?"); vals.append(note)
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(int(time.time() * 1000))
    vals.append(id_)
    with db_cursor() as cur:
        cur.execute(f"UPDATE custom_proxies SET {', '.join(sets)} WHERE id=?", vals)


def delete_custom_proxy(id_: int) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM custom_proxies WHERE id=?", (id_,))


def render_custom_proxies_yaml() -> str:
    """把所有启用的 custom_proxies 拼成 mihomo proxy-providers 用的 yaml。"""
    import yaml as _yaml  # type: ignore

    rows = list_custom_proxies(enabled_only=True)
    proxies: list[dict] = []
    for r in rows:
        full = get_custom_proxy(r["id"])
        if not full:
            continue
        try:
            node = _yaml.safe_load(full["config_yaml"])
            if isinstance(node, dict):
                # 若 yaml 里没 name 用 DB name
                node.setdefault("name", r["name"])
                proxies.append(node)
        except Exception:
            continue
    return _yaml.safe_dump({"proxies": proxies}, allow_unicode=True, sort_keys=False)


# ====== tags ======

def create_tag(name: str, color: str = "#58a6ff") -> int:
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
            (name, color, now),
        )
        return cur.lastrowid


def list_tags_with_count() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT t.id, t.name, t.color, t.created_at, "
            "       COUNT(pt.proxy_id) AS proxy_count "
            "FROM tags t LEFT JOIN proxy_tags pt ON pt.tag_id=t.id "
            "GROUP BY t.id ORDER BY t.name"
        )
        return [dict(r) for r in cur.fetchall()]


def get_tag(tag_id: int) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM tags WHERE id=?", (tag_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_tag_by_name(name: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM tags WHERE name=?", (name,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_tag(tag_id: int, *, name: str | None = None, color: str | None = None) -> None:
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if color is not None:
        sets.append("color=?"); vals.append(color)
    if not sets:
        return
    vals.append(tag_id)
    with db_cursor() as cur:
        cur.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id=?", vals)


def delete_tag(tag_id: int) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM tags WHERE id=?", (tag_id,))


def set_proxy_tags(proxy_id: int, tag_ids: list[int]) -> None:
    """替换节点的标签列表。"""
    with db_cursor() as cur:
        cur.execute("DELETE FROM proxy_tags WHERE proxy_id=?", (proxy_id,))
        for tid in set(tag_ids):
            cur.execute(
                "INSERT OR IGNORE INTO proxy_tags (proxy_id, tag_id) VALUES (?, ?)",
                (proxy_id, tid),
            )


def get_proxy_tags(proxy_id: int) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT t.id, t.name, t.color FROM tags t "
            "JOIN proxy_tags pt ON pt.tag_id=t.id "
            "WHERE pt.proxy_id=? ORDER BY t.name",
            (proxy_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def bulk_get_proxy_tags() -> dict[str, list[dict[str, Any]]]:
    """{proxy_name: [{id, name, color}, ...]}  按 name 关联避免 mihomo/custom id 冲突"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT cp.name, t.id, t.name AS tag_name, t.color FROM proxy_tags pt "
            "JOIN custom_proxies cp ON cp.id = pt.proxy_id "
            "JOIN tags t ON t.id = pt.tag_id "
            "ORDER BY t.name"
        )
        rows = cur.fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["name"], []).append({"id": r["id"], "name": r["tag_name"], "color": r["color"]})
    return out


def add_tag_to_proxies(proxy_ids: list[int], tag_ids: list[int]) -> int:
    """批量给多个节点打多个标签，返回写入行数。"""
    if not proxy_ids or not tag_ids:
        return 0
    n = 0
    with db_cursor() as cur:
        for pid in proxy_ids:
            for tid in tag_ids:
                cur.execute(
                    "INSERT OR IGNORE INTO proxy_tags (proxy_id, tag_id) VALUES (?, ?)",
                    (pid, tid),
                )
                if cur.rowcount > 0:
                    n += 1
    return n


# ====== external_subscriptions ======

def create_external_sub(
    name: str, url: str, *, user_agent: str | None, fmt: str,
    enabled: bool, sync_mode: str, sync_interval_min: int,
    sync_time: str, sync_weekday: int,
    default_tag_ids: list[int] | None,
    name_filter_regex: str | None,
) -> int:
    now = int(time.time() * 1000)
    tag_json = json.dumps(default_tag_ids or [])
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO external_subscriptions "
            "(name, url, user_agent, fmt, enabled, "
            " sync_mode, sync_interval_min, sync_time, sync_weekday, "
            " default_tag_ids, name_filter_regex, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, url, user_agent, fmt, 1 if enabled else 0,
             sync_mode, sync_interval_min, sync_time, sync_weekday,
             tag_json, name_filter_regex, now, now),
        )
        return cur.lastrowid


def update_external_sub(
    sub_id: int, *, name: str | None = None, url: str | None = None,
    user_agent: str | None = None, fmt: str | None = None,
    enabled: int | None = None, sync_mode: str | None = None,
    sync_interval_min: int | None = None, sync_time: str | None = None,
    sync_weekday: int | None = None,
    default_tag_ids: list[int] | None = None,
    name_filter_regex: str | None = None,
) -> None:
    sets, vals = [], []
    if name is not None:                  sets.append("name=?");            vals.append(name)
    if url is not None:                   sets.append("url=?");             vals.append(url)
    if user_agent is not None:            sets.append("user_agent=?");      vals.append(user_agent)
    if fmt is not None:                   sets.append("fmt=?");             vals.append(fmt)
    if enabled is not None:               sets.append("enabled=?");         vals.append(1 if enabled else 0)
    if sync_mode is not None:             sets.append("sync_mode=?");       vals.append(sync_mode)
    if sync_interval_min is not None:     sets.append("sync_interval_min=?"); vals.append(sync_interval_min)
    if sync_time is not None:             sets.append("sync_time=?");       vals.append(sync_time)
    if sync_weekday is not None:          sets.append("sync_weekday=?");    vals.append(sync_weekday)
    if default_tag_ids is not None:       sets.append("default_tag_ids=?"); vals.append(json.dumps(default_tag_ids))
    if name_filter_regex is not None:     sets.append("name_filter_regex=?"); vals.append(name_filter_regex)
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(int(time.time() * 1000))
    vals.append(sub_id)
    with db_cursor() as cur:
        cur.execute(f"UPDATE external_subscriptions SET {', '.join(sets)} WHERE id=?", vals)


def delete_external_sub(sub_id: int) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM external_subscriptions WHERE id=?", (sub_id,))


def get_external_sub(sub_id: int) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM external_subscriptions WHERE id=?", (sub_id,))
        row = cur.fetchone()
        return _post_external_sub(dict(row)) if row else None


def get_external_sub_by_name(name: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM external_subscriptions WHERE name=?", (name,))
        row = cur.fetchone()
        return _post_external_sub(dict(row)) if row else None


def list_external_subs() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM external_subscriptions ORDER BY enabled DESC, id"
        )
        return [_post_external_sub(dict(r)) for r in cur.fetchall()]


def list_enabled_external_subs() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM external_subscriptions WHERE enabled=1 ORDER BY id"
        )
        return [_post_external_sub(dict(r)) for r in cur.fetchall()]


def update_external_sub_sync_result(
    sub_id: int, *, ok: bool, error: str | None,
    added_count: int, traffic: dict[str, int | None],
) -> None:
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "UPDATE external_subscriptions SET "
            "  last_sync_ts=?, last_sync_status=?, last_sync_error=?, last_sync_node_count=?,"
            "  traffic_upload=?, traffic_download=?, traffic_total=?, traffic_expire_ts=?,"
            "  updated_at=? "
            "WHERE id=?",
            (now, "ok" if ok else "fail", error, added_count,
             traffic.get("upload"), traffic.get("download"),
             traffic.get("total"), traffic.get("expire"),
             now, sub_id),
        )


def reset_external_sub_traffic(sub_id: int) -> None:
    """管理员手工重置流量（机场面板通常月复一日一起算，这里只清空 local cache）。"""
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute(
            "UPDATE external_subscriptions SET "
            "  traffic_upload=NULL, traffic_download=NULL, traffic_total=NULL, traffic_expire_ts=NULL,"
            "  updated_at=? WHERE id=?",
            (now, sub_id),
        )


def _post_external_sub(row: dict[str, Any]) -> dict[str, Any]:
    """把 default_tag_ids JSON 反序列化，添加节点计数（用 bulk_get_proxy_tags 算）"""
    if row.get("default_tag_ids"):
        try:
            row["default_tag_ids"] = json.loads(row["default_tag_ids"])
        except Exception:
            row["default_tag_ids"] = []
    else:
        row["default_tag_ids"] = []
    return row

def upsert_admin(username: str, password_hash: str) -> None:
    now = int(time.time() * 1000)
    with db_cursor() as cur:
        cur.execute("SELECT id FROM admins WHERE username=?", (username,))
        if cur.fetchone():
            cur.execute(
                "UPDATE admins SET password_hash=? WHERE username=?",
                (password_hash, username),
            )
        else:
            cur.execute(
                "INSERT INTO admins (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, now),
            )


def get_admin(username: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash FROM admins WHERE username=?",
            (username,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ====== 公开视图聚合 ======

def public_view() -> list[dict[str, Any]]:
    """组拼：proxy + 最新探测 + 最近 N 次延迟柱状数据 + 24h 可用率。"""
    import yaml as _yaml
    from .parser import guess_country
    proxies = list_proxies()
    if not proxies:
        return []
    latest = bulk_latest_probes()
    recent = bulk_recent_delays(limit=30)
    avail = bulk_availability(hours=24)
    # 对 source='custom' 的节点，从 yaml 补 country（proxies 表里没存）
    custom_yaml_cache: dict[str, dict] = {}
    from .parser import parse_uri  # noqa
    def _load_yaml(name: str) -> dict | None:
        if name in custom_yaml_cache:
            return custom_yaml_cache[name]
        cp = get_custom_proxy_by_name(name)
        if not cp:
            custom_yaml_cache[name] = None
            return None
        try:
            y = _yaml.safe_load(cp["config_yaml"])
            custom_yaml_cache[name] = y if isinstance(y, dict) else None
        except Exception:
            custom_yaml_cache[name] = None
        return custom_yaml_cache[name]
    result = []
    for p in proxies:
        lp = latest.get(p["id"])
        if lp and lp["success"]:
            status = "up"
            delay = lp["delay_ms"]
        elif lp and not lp["success"]:
            status = "down"
            delay = None
        else:
            status = "unknown"
            delay = None
        country = p["country"]
        if not country and p.get("source") == "custom":
            y = _load_yaml(p["name"])
            if y and isinstance(y, dict):
                country = y.get("country") or guess_country(p["name"])
        result.append({
            "id": p["id"],
            "name": p["name"],
            "group": p["group_name"],
            "country": country,
            "type": p["type"],
            "status": status,
            "delay": delay,
            "last_check_ts": lp["ts"] if lp else None,
            "last_error": lp["error"] if lp and not lp["success"] else None,
            "recent_delays": recent.get(p["id"], []),
            "availability_24h": avail.get(p["id"]),
        })
    return result


def bulk_recent_delays(limit: int = 30) -> dict[int, list[int]]:
    """{proxy_id: [最近 limit 次成功探测的 delay 列表，按时间正序]}"""
    with db_cursor() as cur:
        cur.execute(
            "SELECT proxy_id, ts, delay_ms FROM probes "
            "WHERE success=1 AND delay_ms IS NOT NULL "
            "ORDER BY ts DESC"
        )
        # 反转成时间正序 + 取前 limit
        from collections import defaultdict
        d = defaultdict(list)
        for proxy_id, ts, delay_ms in cur.fetchall():
            if len(d[proxy_id]) < limit:
                d[proxy_id].append(delay_ms)
        # 反转成时间正序
        for k in d:
            d[k].reverse()
        return dict(d)


def bulk_availability(hours: int = 24) -> dict[int, float]:
    """{proxy_id: 最近 N 小时可用率 0~1}（无任何探测返回 None）"""
    cutoff = int((time.time() - hours * 3600) * 1000)
    with db_cursor() as cur:
        cur.execute(
            "SELECT proxy_id, "
            "  SUM(success) AS ok, "
            "  COUNT(*) AS total "
            "FROM probes WHERE ts >= ? GROUP BY proxy_id",
            (cutoff,),
        )
        out: dict[int, float] = {}
        for r in cur.fetchall():
            if r["total"]:
                out[r["proxy_id"]] = round(r["ok"] / r["total"], 4)
        return out