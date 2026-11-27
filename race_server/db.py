"""SQLite 连接管理、建表脚本与事务包装。

并发模型要点：
- WAL 模式允许读写并发；
- 所有写操作一律使用 ``BEGIN IMMEDIATE`` 立刻获取写锁，
  把“接手 / 替换指令 / 关回合”等临界区整体串行化；
- 拿不到锁时等待 busy_timeout 并在应用层做有限次重试；
- 每个请求使用独立连接，事务提交/回滚后立即关闭。
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from . import config

# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS games (
        game_id               TEXT PRIMARY KEY,
        name                  TEXT NOT NULL,
        width                 INTEGER NOT NULL,
        height                INTEGER NOT NULL,
        blocked_json          TEXT NOT NULL DEFAULT '[]',
        finish_json           TEXT NOT NULL DEFAULT '[]',
        turn_duration_seconds INTEGER NOT NULL DEFAULT 60,
        status                TEXT NOT NULL DEFAULT 'setup'
            CHECK (status IN ('setup', 'active', 'finished')),
        current_turn_no       INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS players (
        player_id         TEXT PRIMARY KEY,
        game_id           TEXT NOT NULL REFERENCES games(game_id),
        name              TEXT NOT NULL,
        secret_hash       TEXT NOT NULL,
        x                 INTEGER,
        y                 INTEGER,
        finished          INTEGER NOT NULL DEFAULT 0,
        finished_turn     INTEGER,
        device_generation INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        UNIQUE (game_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id   TEXT PRIMARY KEY,          -- 设备令牌的哈希值
        game_id     TEXT NOT NULL REFERENCES games(game_id),
        player_id   TEXT NOT NULL REFERENCES players(player_id),
        generation  INTEGER NOT NULL,
        revoked_at  TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turns (
        game_id          TEXT NOT NULL REFERENCES games(game_id),
        turn_no          INTEGER NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        started_at       TEXT NOT NULL,
        deadline         TEXT NOT NULL,
        closed_at        TEXT,
        start_board_json TEXT NOT NULL,
        announcement_json TEXT,
        PRIMARY KEY (game_id, turn_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS command_revisions (
        revision_id       TEXT PRIMARY KEY,
        game_id           TEXT NOT NULL REFERENCES games(game_id),
        turn_no           INTEGER NOT NULL,
        player_id         TEXT NOT NULL REFERENCES players(player_id),
        direction         TEXT NOT NULL
            CHECK (direction IN ('n', 's', 'e', 'w', 'wait')),
        sequence          INTEGER NOT NULL,
        device_generation INTEGER NOT NULL,
        created_at        TEXT NOT NULL,
        UNIQUE (game_id, turn_no, player_id, sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_devices_player ON devices(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_turn ON command_revisions(game_id, turn_no)",
    "CREATE INDEX IF NOT EXISTS idx_turns_game ON turns(game_id)",
)


def get_conn() -> sqlite3.Connection:
    """打开一个配置好的 SQLite 连接（autocommit 模式，事务手动管理）。"""
    conn = sqlite3.connect(
        config.database_path(),
        timeout=10.0,
        isolation_level=None,  # 显式 BEGIN/COMMIT，禁止驱动自动开事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema() -> None:
    """建表并初始化实例级密钥（用于哈希设备令牌/接手密钥）。"""
    import secrets

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('pepper', ?)",
                (secrets.token_hex(32),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get_pepper(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='pepper'").fetchone()
    if row is None:  # 理论上 init_schema 已保证存在
        raise RuntimeError("数据库未初始化，请先调用 init_schema()")
    return row["value"]


def run_transaction(fn: Callable[[sqlite3.Connection], Any], retries: int = 60) -> Any:
    """在 ``BEGIN IMMEDIATE`` 事务中执行 ``fn(conn)``，锁冲突时重试。

    写事务在 SQLite 中天然串行化：同一时刻只有一个 IMMEDIATE 事务持有写锁，
    因此事务内读到的状态与写入的裁定结果构成一个线性一致的临界区。
    """
    conn = get_conn()
    try:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:  # 写锁被占
                last_error = exc
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(min(0.05, 0.005 * (attempt + 1)))
        assert last_error is not None
        raise last_error
    finally:
        conn.close()
