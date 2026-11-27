"""积分赛业务逻辑：账号、报名/撤回、取消与查询。

一致性要点：
- 所有状态变更（含积分扣占/退回/发放）都在 ``BEGIN IMMEDIATE`` 事务内完成，
  SQLite 单写者串行化保证跨场同时报名也不会超用同一份积分；
- 报名/撤回按 (game_id, account_id, request_id) 幂等：重试返回首次处理结果，
  不重复占用或返还；旧撤回的重试命中历史记录，不会撤掉后来的报名；
- 结算（全员完赛平分奖池）与取消（退回全部占用）都只能在各自终局转换的
  事务里发生一次，请求重试或服务重启不会多发奖或多退款。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import get_conn, get_pepper, run_transaction
from .security import hash_secret, new_token
from .services import (
    ServiceError,
    _credit_points,
    _debit_points,
    _effective_status,
    _get_game,
    _get_stakes,
    _iso,
    _loads,
    _now,
    _validate_map,
)


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
def _authenticate_account(conn: sqlite3.Connection, secret: str) -> sqlite3.Row:
    pepper = get_pepper(conn)
    row = conn.execute(
        "SELECT * FROM accounts WHERE secret_hash=?", (hash_secret(pepper, secret),)
    ).fetchone()
    if row is None:
        raise ServiceError(401, "bad_account_secret", "账号密钥错误")
    return row


def create_account(req: Any) -> dict[str, Any]:
    """裁定员创建选手账号：非负整数初始积分，密钥仅本次返回。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        pepper = get_pepper(conn)
        existing = conn.execute(
            "SELECT 1 FROM accounts WHERE name=?", (req.name,)
        ).fetchone()
        if existing is not None:
            raise ServiceError(409, "account_name_taken", f"账号名 {req.name} 已被占用")
        account_id = new_token(12)
        secret = new_token(24)
        now = _iso(_now())
        conn.execute(
            "INSERT INTO accounts(account_id, name, secret_hash, balance, created_at) "
            "VALUES (?,?,?,?,?)",
            (account_id, req.name, hash_secret(pepper, secret), req.initial_points, now),
        )
        if req.initial_points > 0:
            conn.execute(
                """INSERT INTO point_ledger(account_id, game_id, kind, amount,
                                            balance_after, request_key, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    account_id, None, "initial", req.initial_points,
                    req.initial_points, f"initial:{account_id}", "初始积分", now,
                ),
            )
        return {
            "account_id": account_id,
            "name": req.name,
            "account_secret": secret,
            "available_points": req.initial_points,
        }

    return run_transaction(tx)


def list_accounts() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT account_id, name, balance, created_at FROM accounts ORDER BY created_at"
        ).fetchall()
        return [
            {
                "account_id": r["account_id"],
                "name": r["name"],
                "available_points": r["balance"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_account_me(secret: str) -> dict[str, Any]:
    """选手自查：可用积分、各场占用与全部收支流水。"""
    conn = get_conn()
    try:
        account = _authenticate_account(conn, secret)
        holds = conn.execute(
            """SELECT e.game_id, g.name AS game_name, s.entry_fee, e.enrolled_at
               FROM enrollments e
               JOIN games g ON g.game_id = e.game_id
               JOIN stakes_games s ON s.game_id = e.game_id
               WHERE e.account_id=? AND e.status='enrolled'
               ORDER BY e.enrolled_at""",
            (account["account_id"],),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM point_ledger WHERE account_id=? ORDER BY entry_id",
            (account["account_id"],),
        ).fetchall()
        return {
            "account_id": account["account_id"],
            "name": account["name"],
            "available_points": account["balance"],
            "holds": [
                {
                    "game_id": h["game_id"],
                    "game_name": h["game_name"],
                    "entry_fee": h["entry_fee"],
                    "enrolled_at": h["enrolled_at"],
                }
                for h in holds
            ],
            "ledger": [
                {
                    "entry_id": e["entry_id"],
                    "kind": e["kind"],
                    "game_id": e["game_id"],
                    "amount": e["amount"],
                    "balance_after": e["balance_after"],
                    "note": e["note"],
                    "created_at": e["created_at"],
                }
                for e in ledger
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 积分赛创建（沿用现有地图与回合规则）
# ---------------------------------------------------------------------------
def create_stakes_game(req: Any) -> dict[str, Any]:
    _validate_map(req.width, req.height, req.blocked, req.finish)
    if len(set(req.roster)) != len(req.roster):
        raise ServiceError(422, "duplicate_account", "参赛名单中的账号不可重复")

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        accounts: dict[str, sqlite3.Row] = {}
        for name in req.roster:
            row = conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ServiceError(422, "account_not_found", f"账号 {name} 不存在")
            accounts[name] = row

        game_id = new_token(12)
        now = _iso(_now())
        conn.execute(
            """INSERT INTO games(game_id, name, width, height, blocked_json, finish_json,
                                 turn_duration_seconds, status, current_turn_no,
                                 created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'setup', 0, ?, ?)""",
            (
                game_id, req.name, req.width, req.height,
                json.dumps(req.blocked), json.dumps(req.finish),
                req.turn_duration_seconds, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO stakes_games(game_id, entry_fee, status, created_at, updated_at) "
            "VALUES (?,?, 'enrolling', ?, ?)",
            (game_id, req.entry_fee, now, now),
        )
        roster: list[dict[str, Any]] = []
        for name in req.roster:
            account = accounts[name]
            player_id = new_token(12)
            # 席位密钥即账号密钥：选手凭本人账号即可接手自己的席位；
            # 不预发设备令牌，首次接手时签发（device_generation 0 -> 1）
            conn.execute(
                """INSERT INTO players(player_id, game_id, name, secret_hash,
                                       device_generation, account_id, created_at)
                   VALUES (?,?,?,?, 0, ?, ?)""",
                (
                    player_id, game_id, account["name"],
                    account["secret_hash"], account["account_id"], now,
                ),
            )
            conn.execute(
                "INSERT INTO stakes_roster(game_id, account_id, player_id, created_at) "
                "VALUES (?,?,?,?)",
                (game_id, account["account_id"], player_id, now),
            )
            roster.append(
                {
                    "account_id": account["account_id"],
                    "name": account["name"],
                    "player_id": player_id,
                }
            )
        return {
            "game_id": game_id,
            "status": "setup",
            "stakes_status": "enrolling",
            "entry_fee": req.entry_fee,
            "width": req.width,
            "height": req.height,
            "blocked": req.blocked,
            "finish": req.finish,
            "turn_duration_seconds": req.turn_duration_seconds,
            "roster": roster,
        }

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 报名 / 撤回（按请求标识幂等）
# ---------------------------------------------------------------------------
def _replay_if_seen(
    conn: sqlite3.Connection, game_id: str, account_id: str, request_id: str, action: str
) -> dict[str, Any] | None:
    """同一请求标识已处理过则返回首次结果；标识被挪作他用则报错。"""
    prev = conn.execute(
        "SELECT * FROM stakes_requests WHERE game_id=? AND account_id=? AND request_id=?",
        (game_id, account_id, request_id),
    ).fetchone()
    if prev is None:
        return None
    if prev["action"] != action:
        raise ServiceError(
            409, "request_id_conflict", "该请求标识已用于其他操作，请更换标识"
        )
    result = _loads(prev["result_json"])
    result["idempotent_replay"] = True
    return result


def _require_stakes_game(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row:
    _get_game(conn, game_id)
    stakes = _get_stakes(conn, game_id)
    if stakes is None:
        raise ServiceError(404, "stakes_not_found", "该比赛不是积分赛")
    return stakes


def enroll(game_id: str, secret: str, request_id: str) -> dict[str, Any]:
    """报名：占用报名费；可用积分不足时不改变报名或积分。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        stakes = _require_stakes_game(conn, game_id)
        replay = _replay_if_seen(conn, game_id, account["account_id"], request_id, "enroll")
        if replay is not None:
            return replay
        if stakes["status"] == "cancelled":
            raise ServiceError(409, "stakes_cancelled", "该积分赛已取消，不能报名")
        if stakes["status"] != "enrolling":
            raise ServiceError(409, "enrollment_closed", "比赛已发车，不再接受报名")
        seat = conn.execute(
            "SELECT 1 FROM stakes_roster WHERE game_id=? AND account_id=?",
            (game_id, account["account_id"]),
        ).fetchone()
        if seat is None:
            raise ServiceError(403, "not_in_roster", "该账号不在本场比赛名单中")
        enr = conn.execute(
            "SELECT * FROM enrollments WHERE game_id=? AND account_id=?",
            (game_id, account["account_id"]),
        ).fetchone()
        if enr is not None and enr["status"] == "enrolled":
            raise ServiceError(409, "already_enrolled", "已报名，请勿重复占用")

        fee = stakes["entry_fee"]
        now = _iso(_now())
        epoch = (enr["epoch"] + 1) if enr is not None else 1
        # 可用积分不足时此处抛错，整个事务回滚，报名与积分都不变
        new_balance = _debit_points(
            conn, account["account_id"], fee, game_id, "hold",
            request_key=f"hold:{game_id}:{account['account_id']}:{epoch}",
            note=f"报名占用（请求 {request_id}）", now=now,
        )
        if enr is None:
            conn.execute(
                "INSERT INTO enrollments(game_id, account_id, epoch, status, enrolled_at) "
                "VALUES (?,?,?,?,?)",
                (game_id, account["account_id"], epoch, "enrolled", now),
            )
        else:
            conn.execute(
                "UPDATE enrollments SET epoch=?, status='enrolled', enrolled_at=?, "
                "closed_at=NULL WHERE game_id=? AND account_id=?",
                (epoch, now, game_id, account["account_id"]),
            )
        result = {
            "game_id": game_id,
            "account_id": account["account_id"],
            "status": "enrolled",
            "epoch": epoch,
            "entry_fee": fee,
            "available_points": new_balance,
            "request_id": request_id,
            "idempotent_replay": False,
        }
        conn.execute(
            """INSERT INTO stakes_requests(game_id, account_id, request_id, action,
                                           epoch, result_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                game_id, account["account_id"], request_id, "enroll",
                epoch, json.dumps(result), now,
            ),
        )
        return result

    return run_transaction(tx)


def withdraw(game_id: str, secret: str, request_id: str) -> dict[str, Any]:
    """撤回：发车前立即恢复这笔可用积分；重试不重复返还。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        stakes = _require_stakes_game(conn, game_id)
        # 旧撤回的重试在这里命中历史记录直接返回，不会撤掉后来的报名
        replay = _replay_if_seen(conn, game_id, account["account_id"], request_id, "withdraw")
        if replay is not None:
            return replay
        if stakes["status"] == "cancelled":
            raise ServiceError(409, "stakes_cancelled", "该积分赛已取消")
        if stakes["status"] != "enrolling":
            raise ServiceError(409, "enrollment_closed", "比赛已发车，不再接受撤回")
        enr = conn.execute(
            "SELECT * FROM enrollments WHERE game_id=? AND account_id=?",
            (game_id, account["account_id"]),
        ).fetchone()
        if enr is None or enr["status"] != "enrolled":
            raise ServiceError(409, "not_enrolled", "当前没有可撤回的报名")

        fee = stakes["entry_fee"]
        now = _iso(_now())
        epoch = enr["epoch"]
        new_balance = _credit_points(
            conn, account["account_id"], fee, game_id, "release",
            request_key=f"release:{game_id}:{account['account_id']}:{epoch}",
            note=f"撤回返还（请求 {request_id}）", now=now,
        )
        conn.execute(
            "UPDATE enrollments SET status='withdrawn', closed_at=? "
            "WHERE game_id=? AND account_id=?",
            (now, game_id, account["account_id"]),
        )
        result = {
            "game_id": game_id,
            "account_id": account["account_id"],
            "status": "withdrawn",
            "epoch": epoch,
            "entry_fee": fee,
            "available_points": new_balance,
            "request_id": request_id,
            "idempotent_replay": False,
        }
        conn.execute(
            """INSERT INTO stakes_requests(game_id, account_id, request_id, action,
                                           epoch, result_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                game_id, account["account_id"], request_id, "withdraw",
                epoch, json.dumps(result), now,
            ),
        )
        return result

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 取消（仅最终完赛前；仍占用的报名积分全部退回）
# ---------------------------------------------------------------------------
def cancel_game(game_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        _get_game(conn, game_id)
        stakes = _get_stakes(conn, game_id)
        if stakes is None:
            raise ServiceError(409, "not_a_stakes_game", "只有积分赛可以取消")
        if stakes["status"] == "cancelled":
            raise ServiceError(409, "stakes_already_cancelled", "该积分赛已取消")
        if stakes["status"] == "finished":
            raise ServiceError(409, "stakes_already_finished", "比赛已完赛结算，不能取消")

        now = _iso(_now())
        fee = stakes["entry_fee"]
        held = conn.execute(
            "SELECT account_id, epoch FROM enrollments "
            "WHERE game_id=? AND status='enrolled'",
            (game_id,),
        ).fetchall()
        refunds: list[dict[str, Any]] = []
        for e in held:
            _credit_points(
                conn, e["account_id"], fee, game_id, "refund",
                request_key=f"refund:{game_id}:{e['account_id']}:{e['epoch']}",
                note="比赛取消退回报名费", now=now,
            )
            refunds.append({"account_id": e["account_id"], "amount": fee})
        conn.execute(
            "UPDATE enrollments SET status='refunded', closed_at=? "
            "WHERE game_id=? AND status='enrolled'",
            (now, game_id),
        )
        conn.execute(
            "UPDATE stakes_games SET status='cancelled', cancelled_at=?, updated_at=? "
            "WHERE game_id=?",
            (now, now, game_id),
        )
        return {
            "game_id": game_id,
            "status": "cancelled",
            "cancelled_at": now,
            "refunded": refunds,
            "refunded_total": fee * len(refunds),
        }

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 积分赛公开查询（终局与分账结果）
# ---------------------------------------------------------------------------
def get_stakes_state(game_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        game = _get_game(conn, game_id)
        stakes = _get_stakes(conn, game_id)
        if stakes is None:
            raise ServiceError(404, "stakes_not_found", "该比赛不是积分赛")
        rows = conn.execute(
            """SELECT r.account_id, a.name, r.player_id, e.status AS enr_status
               FROM stakes_roster r
               JOIN accounts a ON a.account_id = r.account_id
               LEFT JOIN enrollments e
                   ON e.game_id = r.game_id AND e.account_id = r.account_id
               WHERE r.game_id=? ORDER BY r.rowid""",
            (game_id,),
        ).fetchall()
        enrolled = sum(1 for r in rows if r["enr_status"] == "enrolled")
        return {
            "game_id": game_id,
            "game_status": _effective_status(conn, game),
            "status": stakes["status"],
            "entry_fee": stakes["entry_fee"],
            "pot": stakes["entry_fee"] * enrolled,
            "enrolled": enrolled,
            "roster_size": len(rows),
            "roster": [
                {
                    "account_id": r["account_id"],
                    "name": r["name"],
                    "player_id": r["player_id"],
                    "enrollment": r["enr_status"] or "none",
                }
                for r in rows
            ],
            "settlement": _loads(stakes["settlement_json"]),
            "cancelled_at": stakes["cancelled_at"],
        }
    finally:
        conn.close()
