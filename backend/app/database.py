"""SQLite 连接管理。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings


_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.db_path) as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # 轻量级 schema 升级：缺失列时自动添加
        _ensure_columns(conn, "external_subscriptions", {
            "sync_mode": "TEXT NOT NULL DEFAULT 'interval'",
            "sync_time": "TEXT NOT NULL DEFAULT '03:00'",
            "sync_weekday": "INTEGER NOT NULL DEFAULT 0",
            "name_filter_regex": "TEXT",
        })
        conn.commit()


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict[str, str]) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for name, decl in cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_init_db()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_cursor():
    """带自动提交/回滚的 cursor 上下文。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()