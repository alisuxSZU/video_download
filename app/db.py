"""SQLite 持久化层（v0.7.0 会员/支付引入）。

设计取舍：
- 用 Python 标准库 sqlite3，零额外服务/依赖；项目本身单进程 ``--workers 1``，
  模块级单连接 + 一把可重入锁串行化写事务即足够安全。
- 开启 WAL：后台线程（下载 job）与 async 端点可能并发读写，WAL 允许读写不互斥。
- 所有 SQL 一律参数化（``?`` 占位），杜绝注入。
- 建表用 CREATE TABLE IF NOT EXISTS，v1 不引迁移工具。

表：users / auth_tokens / orders / webhook_events / ai_usage（见 docs/MEMBERSHIP.md §4）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id            TEXT PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    password_hash      TEXT NOT NULL,
    stripe_customer_id TEXT,
    member_expire_at   INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    plan_key          TEXT NOT NULL,
    amount_cents      INTEGER NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    stripe_session_id TEXT UNIQUE,
    created_at        INTEGER NOT NULL,
    paid_at           INTEGER
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_usage (
    subject TEXT NOT NULL,
    ymd     TEXT NOT NULL,
    calls   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, ymd)
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id);
"""


def init_db() -> None:
    """幂等建库建表。应用启动时调用一次。"""
    global _conn
    with _lock:
        if _conn is not None:
            return
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn


def _c() -> sqlite3.Connection:
    if _conn is None:  # 允许脚本/测试在未走 lifespan 时直接用
        init_db()
    assert _conn is not None
    return _conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：BEGIN IMMEDIATE 立即拿写锁，提交/回滚由本上下文保证。

    Webhook 履约的「条件更新订单 + 叠加会员时长」必须包在同一事务里。
    """
    conn = _c()
    with _lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with _lock:
        return _c().execute(sql, params).fetchone()


def query_all(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return _c().execute(sql, params).fetchall()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """执行单条写 SQL（自动提交），返回 cursor.rowcount。"""
    with _lock:
        cur = _c().execute(sql, params)
        _c().commit()
        return cur.rowcount


def now_ts() -> int:
    return int(time.time())
