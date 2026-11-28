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
        account_id        TEXT,          -- 积分赛席位对应的账号；普通比赛为 NULL
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
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        secret_hash TEXT NOT NULL,
        balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stakes_games (
        game_id         TEXT PRIMARY KEY REFERENCES games(game_id),
        entry_fee       INTEGER NOT NULL CHECK (entry_fee > 0),
        status          TEXT NOT NULL DEFAULT 'enrolling'
            CHECK (status IN ('enrolling', 'active', 'finished', 'cancelled')),
        settlement_json TEXT,
        cancelled_at    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stakes_roster (
        game_id    TEXT NOT NULL REFERENCES games(game_id),
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        player_id  TEXT NOT NULL REFERENCES players(player_id),
        created_at TEXT NOT NULL,
        PRIMARY KEY (game_id, account_id),
        UNIQUE (game_id, player_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enrollments (
        game_id     TEXT NOT NULL REFERENCES games(game_id),
        account_id  TEXT NOT NULL REFERENCES accounts(account_id),
        epoch       INTEGER NOT NULL,
        status      TEXT NOT NULL
            CHECK (status IN ('enrolled', 'withdrawn', 'refunded', 'settled')),
        enrolled_at TEXT NOT NULL,
        closed_at   TEXT,
        PRIMARY KEY (game_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stakes_requests (
        game_id     TEXT NOT NULL,
        account_id  TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        action      TEXT NOT NULL CHECK (action IN ('enroll', 'withdraw')),
        epoch       INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (game_id, account_id, request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point_ledger (
        entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id    TEXT NOT NULL REFERENCES accounts(account_id),
        game_id       TEXT,
        kind          TEXT NOT NULL
            CHECK (kind IN ('initial', 'hold', 'release', 'refund', 'payout')),
        amount        INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        request_key   TEXT NOT NULL UNIQUE,
        note          TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------------------
    # 八人双败杯
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS cups (
        cup_id                TEXT PRIMARY KEY,
        name                  TEXT NOT NULL,
        width                 INTEGER NOT NULL,
        height                INTEGER NOT NULL,
        blocked_json          TEXT NOT NULL DEFAULT '[]',
        finish_json           TEXT NOT NULL DEFAULT '[]',
        turn_duration_seconds INTEGER NOT NULL DEFAULT 60,
        -- 签表版本：每次开赛/重赛自增，用于拒绝迟到的旧请求
        revision              INTEGER NOT NULL DEFAULT 0,
        champion_withdrawn    INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cup_seeds (
        cup_id     TEXT NOT NULL REFERENCES cups(cup_id),
        seed_no    INTEGER NOT NULL,       -- 原始签位 1..8，越大越靠后
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        name       TEXT NOT NULL,
        PRIMARY KEY (cup_id, seed_no),
        UNIQUE (cup_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cup_games (
        game_id            TEXT PRIMARY KEY REFERENCES games(game_id),
        cup_id             TEXT NOT NULL REFERENCES cups(cup_id),
        match_no           INTEGER NOT NULL,
        order_index        INTEGER NOT NULL,   -- 该场次的第几局（1 起）
        slot               TEXT NOT NULL CHECK (slot IN ('regular', 'decider')),
        home_seed          INTEGER,
        away_seed          INTEGER,
        winner_player_id   TEXT,
        loser_player_id    TEXT,
        valid              INTEGER NOT NULL DEFAULT 1,
        invalidated_reason TEXT,
        invalidated_at     TEXT,
        created_at         TEXT NOT NULL,
        UNIQUE (cup_id, match_no, order_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cup_invalidations (
        invalidation_id       TEXT PRIMARY KEY,
        cup_id                TEXT NOT NULL REFERENCES cups(cup_id),
        rematch_request_id    TEXT NOT NULL,
        invalidated_game_id   TEXT NOT NULL,
        match_no              INTEGER NOT NULL,
        scope_slot            TEXT NOT NULL,
        reason                TEXT NOT NULL,
        turn_no               INTEGER,
        propagated_json       TEXT NOT NULL DEFAULT '[]',
        champion_withdrawn    INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL,
        UNIQUE (cup_id, rematch_request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cup_requests (
        cup_id      TEXT NOT NULL REFERENCES cups(cup_id),
        kind        TEXT NOT NULL CHECK (kind IN ('start', 'rematch')),
        request_id  TEXT NOT NULL,
        match_no    INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (cup_id, kind, request_id)
    )
    """,
    # ------------------------------------------------------------------
    # 练习赛大厅
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS practice_rooms (
        room_id                 TEXT PRIMARY KEY,
        name                    TEXT NOT NULL,
        width                   INTEGER NOT NULL,
        height                  INTEGER NOT NULL,
        blocked_json            TEXT NOT NULL DEFAULT '[]',
        finish_json             TEXT NOT NULL DEFAULT '[]',
        turn_duration_seconds   INTEGER NOT NULL DEFAULT 60,
        -- 配对确认时限：邀请发出后双方须在此秒数内确认
        confirm_timeout_seconds INTEGER NOT NULL,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_registrations (
        room_id    TEXT NOT NULL REFERENCES practice_rooms(room_id),
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        -- 每次重新报名自增；旧请求按 (room_id, account_id, request_id) 幂等隔离
        epoch      INTEGER NOT NULL,
        -- 入队顺序号（房间级单调递增）；暂停/恢复不改变，重新报名取新号
        seq        INTEGER NOT NULL,
        status     TEXT NOT NULL
            CHECK (status IN ('queued', 'paused', 'invited',
                              'consumed', 'withdrawn', 'removed')),
        request_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (room_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_requests (
        room_id     TEXT NOT NULL,
        account_id  TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        action      TEXT NOT NULL CHECK (action IN ('register', 'withdraw')),
        epoch       INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (room_id, account_id, request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_invitations (
        invitation_id TEXT PRIMARY KEY,
        room_id       TEXT NOT NULL REFERENCES practice_rooms(room_id),
        status        TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'started', 'declined', 'expired')),
        -- 确认截止（服务端时间）；过期邀请由后续操作懒处理，不能复活
        deadline      TEXT NOT NULL,
        game_id       TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_invitation_seats (
        invitation_id TEXT NOT NULL REFERENCES practice_invitations(invitation_id),
        account_id    TEXT NOT NULL REFERENCES accounts(account_id),
        -- 邀请对应的报名 epoch：过期/解除时只影响该次报名，不碰后来的新报名
        reg_epoch     INTEGER NOT NULL,
        response      TEXT NOT NULL DEFAULT 'pending'
            CHECK (response IN ('pending', 'confirmed', 'declined')),
        responded_at  TEXT,
        player_id     TEXT,
        PRIMARY KEY (invitation_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_pair_requests (
        room_id     TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (room_id, request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_games (
        game_id         TEXT PRIMARY KEY REFERENCES games(game_id),
        room_id         TEXT NOT NULL REFERENCES practice_rooms(room_id),
        invitation_id   TEXT NOT NULL REFERENCES practice_invitations(invitation_id),
        home_account_id TEXT NOT NULL REFERENCES accounts(account_id),
        away_account_id TEXT NOT NULL REFERENCES accounts(account_id),
        status          TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'finished', 'ended')),
        ended_at        TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_devices_player ON devices(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_turn ON command_revisions(game_id, turn_no)",
    "CREATE INDEX IF NOT EXISTS idx_turns_game ON turns(game_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_account ON point_ledger(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_enrollments_game ON enrollments(game_id)",
    "CREATE INDEX IF NOT EXISTS idx_cup_games_cup ON cup_games(cup_id, match_no)",
    "CREATE INDEX IF NOT EXISTS idx_cup_invalidations_cup ON cup_invalidations(cup_id)",
    "CREATE INDEX IF NOT EXISTS idx_practice_reg_account ON practice_registrations(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_practice_inv_room ON practice_invitations(room_id)",
    "CREATE INDEX IF NOT EXISTS idx_practice_seats_account "
    "ON practice_invitation_seats(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_practice_games_room ON practice_games(room_id)",
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
            # 既有数据库的轻量迁移：players 补 account_id 列（老数据为 NULL）
            player_cols = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
            if "account_id" not in player_cols:
                conn.execute("ALTER TABLE players ADD COLUMN account_id TEXT")
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
