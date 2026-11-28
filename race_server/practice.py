"""练习赛大厅：练习房、排队报名、配对邀请与练习对局。

设计要点
--------
- **房间与排队**：裁定员开设练习房（指定地图、回合时长与配对确认时限）。
  选手凭现有账号在任意多个房间排队；同一房间同一账号至多一条进行中的
  报名，撤销/被退队/被用掉后可重新报名（新 epoch、新的队尾序号）。
- **配对**：客户端可请求某房间配对一次：从该房当前可配对（queued）的
  报名中按入队序号取最早两位，不足两位则不发邀请。配对按
  ``(room_id, request_id)`` 幂等，重试返回首次结果，不会重复发邀请。
- **占用不变量**：同一账号同一时刻至多持有一份待确认邀请或一场未结束的
  练习赛。邀请发出时，双方在其他房间的全部报名暂停（paused，保留原
  序号），配对只取 queued 报名，天然跳过暂停者。
- **邀请确认**：双方都须在时限（服务端时间）内凭本人账号确认同一份邀请；
  第二人确认时在同一事务内开出一局可玩的对局（席位密钥即账号密钥）。
  有人拒绝则解除邀请，仅拒绝者从该房退队；到期未配成则仅未确认者从该房
  退队；其余报名恢复原状态与原顺序，无须重新排队。过期邀请由后续操作
  懒处理（无后台任务），迟到的确认/拒绝一律 409，不能复活。
- **对局**：练习对局沿用既有回合玩法；全员完赛（关回合事务内）或裁定员
  提前结束时，双方账号在其他房间的暂停报名恢复。提前结束不算完赛，
  保留已公开公告，不再接手/提交/裁定新回合。
- **一致性**：所有状态变更在 ``BEGIN IMMEDIATE`` 事务内完成；报名/撤销/
  配对/确认/拒绝均可安全重试，重试旧请求不会消耗新的排队名额或影响新
  邀请；服务重启后由 SQLite 持久状态继续办理。普通比赛、积分赛和杯赛
  不参与大厅占用，练习赛不改积分或签表。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .db import run_transaction
from .security import new_token
from .services import (
    ServiceError,
    _get_game,
    _iso,
    _loads,
    _now,
    _validate_map,
)
from .stakes import _authenticate_account


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------
def _get_room(conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM practice_rooms WHERE room_id=?", (room_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "room_not_found", f"练习房 {room_id} 不存在")
    return row


def _get_invitation(conn: sqlite3.Connection, invitation_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM practice_invitations WHERE invitation_id=?", (invitation_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "invitation_not_found", "配对邀请不存在")
    return row


def _get_practice_game_row(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM practice_games WHERE game_id=?", (game_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "practice_not_found", "该对局不是练习赛")
    return row


def _get_registration(
    conn: sqlite3.Connection, room_id: str, account_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM practice_registrations WHERE room_id=? AND account_id=?",
        (room_id, account_id),
    ).fetchone()


def _account_occupied(conn: sqlite3.Connection, account_id: str) -> bool:
    """该账号当前是否持有待确认邀请或未结束的练习赛（占用大厅配对资格）。"""
    row = conn.execute(
        """SELECT 1 FROM practice_invitations i
           JOIN practice_invitation_seats s ON s.invitation_id = i.invitation_id
           WHERE s.account_id=? AND i.status='pending' LIMIT 1""",
        (account_id,),
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        """SELECT 1 FROM practice_games
           WHERE status='active' AND (home_account_id=? OR away_account_id=?)
           LIMIT 1""",
        (account_id, account_id),
    ).fetchone()
    return row is not None


def _pause_account_registrations(
    conn: sqlite3.Connection, account_id: str, now: str
) -> None:
    """账号收到邀请/进入对局时：其全部 queued 报名暂停（保留原序号）。"""
    conn.execute(
        "UPDATE practice_registrations SET status='paused', updated_at=? "
        "WHERE account_id=? AND status='queued'",
        (now, account_id),
    )


def _resume_account_registrations(
    conn: sqlite3.Connection, account_id: str, now: str
) -> None:
    """账号空出时：其全部暂停报名恢复原状态与原顺序，无须重新排队。"""
    conn.execute(
        "UPDATE practice_registrations SET status='queued', updated_at=? "
        "WHERE account_id=? AND status='paused'",
        (now, account_id),
    )


# ---------------------------------------------------------------------------
# 邀请的解除与过期（懒处理，无后台任务）
# ---------------------------------------------------------------------------
def _resolve_expired(conn: sqlite3.Connection, inv: sqlite3.Row, now: str) -> None:
    """到期未配成：仅未确认者从该房退队，已确认者与其他报名恢复原顺序。"""
    seats = conn.execute(
        "SELECT * FROM practice_invitation_seats WHERE invitation_id=?",
        (inv["invitation_id"],),
    ).fetchall()
    for seat in seats:
        target = "queued" if seat["response"] == "confirmed" else "removed"
        conn.execute(
            "UPDATE practice_registrations SET status=?, updated_at=? "
            "WHERE room_id=? AND account_id=? AND epoch=? AND status='invited'",
            (target, now, inv["room_id"], seat["account_id"], seat["reg_epoch"]),
        )
        # 邀请是该账号唯一的占用，解除后其暂停报名全部恢复
        _resume_account_registrations(conn, seat["account_id"], now)
    conn.execute(
        "UPDATE practice_invitations SET status='expired', updated_at=? "
        "WHERE invitation_id=?",
        (now, inv["invitation_id"]),
    )


def _expire_overdue(conn: sqlite3.Connection, now: str) -> None:
    """把所有已到期待确认邀请置为过期（以服务端时间为准）。"""
    rows = conn.execute(
        "SELECT * FROM practice_invitations WHERE status='pending' AND deadline<?",
        (now,),
    ).fetchall()
    for inv in rows:
        _resolve_expired(conn, inv, now)


def _resolve_declined(
    conn: sqlite3.Connection, inv: sqlite3.Row, decliner_account_id: str, now: str
) -> None:
    """有人拒绝：解除邀请，仅拒绝者从该房退队；其余报名恢复原顺序。"""
    seats = conn.execute(
        "SELECT * FROM practice_invitation_seats WHERE invitation_id=?",
        (inv["invitation_id"],),
    ).fetchall()
    for seat in seats:
        target = "removed" if seat["account_id"] == decliner_account_id else "queued"
        conn.execute(
            "UPDATE practice_registrations SET status=?, updated_at=? "
            "WHERE room_id=? AND account_id=? AND epoch=? AND status='invited'",
            (target, now, inv["room_id"], seat["account_id"], seat["reg_epoch"]),
        )
        _resume_account_registrations(conn, seat["account_id"], now)
    conn.execute(
        "UPDATE practice_invitations SET status='declined', updated_at=? "
        "WHERE invitation_id=?",
        (now, inv["invitation_id"]),
    )


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------
def _room_counts(conn: sqlite3.Connection, room_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM practice_registrations "
        "WHERE room_id=? GROUP BY status",
        (room_id,),
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    active_games = conn.execute(
        "SELECT COUNT(*) AS c FROM practice_games WHERE room_id=? AND status='active'",
        (room_id,),
    ).fetchone()["c"]
    return {
        "queued": counts.get("queued", 0),
        "paused": counts.get("paused", 0),
        "invited": counts.get("invited", 0),
        "active_games": active_games,
    }


def _room_summary(conn: sqlite3.Connection, room: sqlite3.Row) -> dict[str, Any]:
    return {
        "room_id": room["room_id"],
        "name": room["name"],
        "width": room["width"],
        "height": room["height"],
        "blocked": _loads(room["blocked_json"]),
        "finish": _loads(room["finish_json"]),
        "turn_duration_seconds": room["turn_duration_seconds"],
        "confirm_timeout_seconds": room["confirm_timeout_seconds"],
        **_room_counts(conn, room["room_id"]),
        "created_at": room["created_at"],
    }


def _practice_game_view(
    conn: sqlite3.Connection, pg: sqlite3.Row, my_account_id: str | None = None
) -> dict[str, Any]:
    room = conn.execute(
        "SELECT name FROM practice_rooms WHERE room_id=?", (pg["room_id"],)
    ).fetchone()
    players = conn.execute(
        "SELECT player_id, name, account_id FROM players WHERE game_id=? ORDER BY rowid",
        (pg["game_id"],),
    ).fetchall()
    my_player_id = None
    player_views = []
    for p in players:
        if my_account_id is not None and p["account_id"] == my_account_id:
            my_player_id = p["player_id"]
        player_views.append(
            {
                "account_id": p["account_id"],
                "name": p["name"],
                "player_id": p["player_id"],
            }
        )
    return {
        "game_id": pg["game_id"],
        "room_id": pg["room_id"],
        "room_name": room["name"] if room else "",
        "invitation_id": pg["invitation_id"],
        "status": pg["status"],
        "players": player_views,
        "my_player_id": my_player_id,
        "created_at": pg["created_at"],
        "ended_at": pg["ended_at"],
    }


def _room_detail(conn: sqlite3.Connection, room: sqlite3.Row) -> dict[str, Any]:
    out = _room_summary(conn, room)
    queue = conn.execute(
        """SELECT r.account_id, a.name, r.seq, r.status, r.created_at
           FROM practice_registrations r
           JOIN accounts a ON a.account_id = r.account_id
           WHERE r.room_id=? AND r.status IN ('queued', 'paused', 'invited')
           ORDER BY r.seq""",
        (room["room_id"],),
    ).fetchall()
    out["queue"] = [
        {
            "account_id": q["account_id"],
            "name": q["name"],
            "seq": q["seq"],
            "status": q["status"],
            "since": q["created_at"],
        }
        for q in queue
    ]
    games = conn.execute(
        "SELECT * FROM practice_games WHERE room_id=? ORDER BY created_at",
        (room["room_id"],),
    ).fetchall()
    out["games"] = [_practice_game_view(conn, g) for g in games]
    return out


def _invitation_view(
    conn: sqlite3.Connection, inv: sqlite3.Row, my_account_id: str | None = None
) -> dict[str, Any]:
    room = conn.execute(
        "SELECT name FROM practice_rooms WHERE room_id=?", (inv["room_id"],)
    ).fetchone()
    seats = conn.execute(
        """SELECT s.account_id, a.name, s.response, s.responded_at, s.player_id
           FROM practice_invitation_seats s
           JOIN accounts a ON a.account_id = s.account_id
           WHERE s.invitation_id=? ORDER BY s.rowid""",
        (inv["invitation_id"],),
    ).fetchall()
    my_response = None
    seat_views = []
    for s in seats:
        if my_account_id is not None and s["account_id"] == my_account_id:
            my_response = s["response"]
        seat_views.append(
            {
                "account_id": s["account_id"],
                "name": s["name"],
                "response": s["response"],
                "responded_at": s["responded_at"],
                "player_id": s["player_id"],
            }
        )
    return {
        "invitation_id": inv["invitation_id"],
        "room_id": inv["room_id"],
        "room_name": room["name"] if room else "",
        "status": inv["status"],
        "deadline": inv["deadline"],
        "game_id": inv["game_id"],
        "seats": seat_views,
        "my_response": my_response,
        "created_at": inv["created_at"],
    }


# ---------------------------------------------------------------------------
# 房间开设与查询
# ---------------------------------------------------------------------------
def create_room(req: Any) -> dict[str, Any]:
    _validate_map(req.width, req.height, req.blocked, req.finish)
    blocked_set = {tuple(c) for c in req.blocked}
    finish_set = {tuple(c) for c in req.finish}
    starts = [
        (0, y)
        for y in range(req.height)
        if (0, y) not in blocked_set and (0, y) not in finish_set
    ]
    if len(starts) < 2:
        raise ServiceError(
            422, "not_enough_start_cells", "左边界至少需要 2 个可通行起点格供两名选手入场"
        )
    if len(finish_set) < 2:
        raise ServiceError(
            422, "not_enough_finish_cells", "两名选手都须完赛，终点格至少需要 2 个"
        )

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        room_id = new_token(12)
        now = _iso(_now())
        conn.execute(
            """INSERT INTO practice_rooms(room_id, name, width, height, blocked_json,
                                          finish_json, turn_duration_seconds,
                                          confirm_timeout_seconds, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                room_id, req.name, req.width, req.height,
                json.dumps(req.blocked), json.dumps(req.finish),
                req.turn_duration_seconds, req.confirm_timeout_seconds, now, now,
            ),
        )
        return _room_detail(conn, _get_room(conn, room_id))

    return run_transaction(tx)


def list_rooms() -> list[dict[str, Any]]:
    def tx(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        # 懒处理过期邀请，保证计数与状态准确
        _expire_overdue(conn, _iso(_now()))
        rows = conn.execute(
            "SELECT * FROM practice_rooms ORDER BY created_at"
        ).fetchall()
        return [_room_summary(conn, r) for r in rows]

    return run_transaction(tx)


def get_room_detail(room_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        room = _get_room(conn, room_id)
        _expire_overdue(conn, _iso(_now()))
        return _room_detail(conn, room)

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 报名 / 撤销（按请求标识幂等）
# ---------------------------------------------------------------------------
def _replay_request(
    conn: sqlite3.Connection,
    room_id: str,
    account_id: str,
    request_id: str,
    action: str,
) -> dict[str, Any] | None:
    """同一请求标识已处理过则返回首次结果；标识被挪作他用则报错。"""
    prev = conn.execute(
        "SELECT * FROM practice_requests "
        "WHERE room_id=? AND account_id=? AND request_id=?",
        (room_id, account_id, request_id),
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


def _store_request(
    conn: sqlite3.Connection,
    room_id: str,
    account_id: str,
    request_id: str,
    action: str,
    epoch: int,
    result: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """INSERT INTO practice_requests(room_id, account_id, request_id, action,
                                         epoch, result_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (room_id, account_id, request_id, action, epoch, json.dumps(result), now),
    )


def register(room_id: str, secret: str, request_id: str) -> dict[str, Any]:
    """报名排队：可在多个房间排队；被占用（有待确认邀请或在赛）时先入队但暂停配对。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        _get_room(conn, room_id)
        replay = _replay_request(
            conn, room_id, account["account_id"], request_id, "register"
        )
        if replay is not None:
            return replay
        now = _iso(_now())
        # 先懒处理过期邀请，占用状态才准确
        _expire_overdue(conn, now)
        reg = _get_registration(conn, room_id, account["account_id"])
        if reg is not None and reg["status"] in ("queued", "paused", "invited"):
            raise ServiceError(409, "already_registered", "已在该房间排队，请勿重复报名")
        status = "paused" if _account_occupied(conn, account["account_id"]) else "queued"
        epoch = (reg["epoch"] + 1) if reg is not None else 1
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM practice_registrations "
            "WHERE room_id=?",
            (room_id,),
        ).fetchone()["s"]
        if reg is None:
            conn.execute(
                """INSERT INTO practice_registrations(room_id, account_id, epoch, seq,
                                                      status, request_id,
                                                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (room_id, account["account_id"], epoch, seq, status, request_id, now, now),
            )
        else:
            conn.execute(
                """UPDATE practice_registrations
                   SET epoch=?, seq=?, status=?, request_id=?, created_at=?, updated_at=?
                   WHERE room_id=? AND account_id=?""",
                (epoch, seq, status, request_id, now, now, room_id, account["account_id"]),
            )
        result = {
            "room_id": room_id,
            "account_id": account["account_id"],
            "status": status,
            "epoch": epoch,
            "seq": seq,
            "request_id": request_id,
            "idempotent_replay": False,
        }
        _store_request(
            conn, room_id, account["account_id"], request_id, "register",
            epoch, result, now,
        )
        return result

    return run_transaction(tx)


def withdraw(room_id: str, secret: str, request_id: str) -> dict[str, Any]:
    """撤销尚未收到邀请的报名（queued/paused）；已收到邀请的报名须先处理邀请。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        _get_room(conn, room_id)
        # 旧撤销的重试在这里命中历史记录直接返回，不会影响后来的报名
        replay = _replay_request(
            conn, room_id, account["account_id"], request_id, "withdraw"
        )
        if replay is not None:
            return replay
        now = _iso(_now())
        _expire_overdue(conn, now)
        reg = _get_registration(conn, room_id, account["account_id"])
        if reg is None or reg["status"] in ("withdrawn", "removed", "consumed"):
            raise ServiceError(409, "not_registered", "当前没有可撤销的报名")
        if reg["status"] == "invited":
            raise ServiceError(
                409, "registration_invited", "报名已收到配对邀请，请先处理邀请"
            )
        conn.execute(
            "UPDATE practice_registrations SET status='withdrawn', updated_at=? "
            "WHERE room_id=? AND account_id=?",
            (now, room_id, account["account_id"]),
        )
        result = {
            "room_id": room_id,
            "account_id": account["account_id"],
            "status": "withdrawn",
            "epoch": reg["epoch"],
            "seq": reg["seq"],
            "request_id": request_id,
            "idempotent_replay": False,
        }
        _store_request(
            conn, room_id, account["account_id"], request_id, "withdraw",
            reg["epoch"], result, now,
        )
        return result

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 配对：每次取该房当前可配对的最早两位；不足两位则不发邀请
# ---------------------------------------------------------------------------
def request_pair(room_id: str, request_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        room = _get_room(conn, room_id)
        prev = conn.execute(
            "SELECT * FROM practice_pair_requests WHERE room_id=? AND request_id=?",
            (room_id, request_id),
        ).fetchone()
        if prev is not None:
            # 重试旧配对请求：返回首次结果（邀请当前状态实时查询），不重复发邀请
            result = _loads(prev["result_json"])
            result["idempotent_replay"] = True
            if result.get("invitation_id"):
                inv = conn.execute(
                    "SELECT * FROM practice_invitations WHERE invitation_id=?",
                    (result["invitation_id"],),
                ).fetchone()
                result["invitation"] = (
                    _invitation_view(conn, inv) if inv is not None else None
                )
            return result
        now_dt = _now()
        now = _iso(now_dt)
        # 过期邀请不能挡住后续配对：先懒处理再选人
        _expire_overdue(conn, now)
        rows = conn.execute(
            "SELECT * FROM practice_registrations "
            "WHERE room_id=? AND status='queued' ORDER BY seq",
            (room_id,),
        ).fetchall()
        candidates: list[sqlite3.Row] = []
        for reg in rows:
            if len(candidates) >= 2:
                break
            if _account_occupied(conn, reg["account_id"]):
                # 防御：被占用账号的报名不应处于 queued，自愈为 paused 并跳过
                conn.execute(
                    "UPDATE practice_registrations SET status='paused', updated_at=? "
                    "WHERE room_id=? AND account_id=? AND status='queued'",
                    (now, room_id, reg["account_id"]),
                )
                continue
            candidates.append(reg)
        if len(candidates) < 2:
            result = {
                "room_id": room_id,
                "paired": False,
                "invitation_id": None,
                "invitation": None,
                "request_id": request_id,
                "idempotent_replay": False,
            }
            conn.execute(
                """INSERT INTO practice_pair_requests(room_id, request_id,
                                                      result_json, created_at)
                   VALUES (?,?,?,?)""",
                (room_id, request_id, json.dumps(result), now),
            )
            return result

        invitation_id = new_token(12)
        deadline = _iso(now_dt + timedelta(seconds=room["confirm_timeout_seconds"]))
        conn.execute(
            """INSERT INTO practice_invitations(invitation_id, room_id, status,
                                                deadline, created_at, updated_at)
               VALUES (?,?, 'pending', ?, ?, ?)""",
            (invitation_id, room_id, deadline, now, now),
        )
        for reg in candidates:
            conn.execute(
                """INSERT INTO practice_invitation_seats(invitation_id, account_id,
                                                         reg_epoch, response)
                   VALUES (?,?,?, 'pending')""",
                (invitation_id, reg["account_id"], reg["epoch"]),
            )
            conn.execute(
                "UPDATE practice_registrations SET status='invited', updated_at=? "
                "WHERE room_id=? AND account_id=?",
                (now, room_id, reg["account_id"]),
            )
            # 双方在其他房间的报名暂停配对（保留原排队顺序）
            _pause_account_registrations(conn, reg["account_id"], now)
        inv = _get_invitation(conn, invitation_id)
        result = {
            "room_id": room_id,
            "paired": True,
            "invitation_id": invitation_id,
            "invitation": _invitation_view(conn, inv),
            "request_id": request_id,
            "idempotent_replay": False,
        }
        conn.execute(
            """INSERT INTO practice_pair_requests(room_id, request_id,
                                                  result_json, created_at)
               VALUES (?,?,?,?)""",
            (room_id, request_id, json.dumps(result), now),
        )
        return result

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 邀请确认 / 拒绝
# ---------------------------------------------------------------------------
def _get_seat(
    conn: sqlite3.Connection, invitation_id: str, account_id: str
) -> sqlite3.Row:
    seat = conn.execute(
        "SELECT * FROM practice_invitation_seats "
        "WHERE invitation_id=? AND account_id=?",
        (invitation_id, account_id),
    ).fetchone()
    if seat is None:
        raise ServiceError(403, "not_invited", "该账号不是此邀请的当事人")
    return seat


def _start_practice_game(
    conn: sqlite3.Connection, inv: sqlite3.Row, now_dt: Any
) -> str:
    """第二人确认时开出一局可玩的对局（与普通比赛同一套回合玩法）。"""
    room = _get_room(conn, inv["room_id"])
    seats = conn.execute(
        "SELECT * FROM practice_invitation_seats WHERE invitation_id=? ORDER BY rowid",
        (inv["invitation_id"],),
    ).fetchall()
    now = _iso(now_dt)
    game_id = new_token(12)
    conn.execute(
        """INSERT INTO games(game_id, name, width, height, blocked_json, finish_json,
                             turn_duration_seconds, status, current_turn_no,
                             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?, 'setup', 0, ?, ?)""",
        (
            game_id, f"{room['name']} 练习赛", room["width"], room["height"],
            room["blocked_json"], room["finish_json"], room["turn_duration_seconds"],
            now, now,
        ),
    )
    player_ids: list[str] = []
    for seat in seats:
        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id=?", (seat["account_id"],)
        ).fetchone()
        player_id = new_token(12)
        # 席位密钥即账号密钥：选手凭本人账号接手本局对应席位
        conn.execute(
            """INSERT INTO players(player_id, game_id, name, secret_hash,
                                   device_generation, account_id, created_at)
               VALUES (?,?,?,?, 0, ?, ?)""",
            (
                player_id, game_id, account["name"], account["secret_hash"],
                account["account_id"], now,
            ),
        )
        conn.execute(
            "UPDATE practice_invitation_seats SET player_id=? "
            "WHERE invitation_id=? AND account_id=?",
            (player_id, inv["invitation_id"], seat["account_id"]),
        )
        player_ids.append(player_id)

    # 布置起点并开启回合 1（与普通比赛开局一致：左边界自上而下）
    blocked = {tuple(c) for c in _loads(room["blocked_json"])}
    finish = {tuple(c) for c in _loads(room["finish_json"])}
    starts = [
        (0, y)
        for y in range(room["height"])
        if (0, y) not in blocked and (0, y) not in finish
    ]
    if len(starts) < len(player_ids):  # 房间创建时已校验，防御性再查
        raise ServiceError(409, "not_enough_start_cells", "起点格不足，无法开局")
    board: dict[str, list[int]] = {}
    for pid, (x, y) in zip(player_ids, starts):
        conn.execute("UPDATE players SET x=?, y=? WHERE player_id=?", (x, y, pid))
        board[pid] = [x, y]
    deadline = _iso(now_dt + timedelta(seconds=room["turn_duration_seconds"]))
    conn.execute(
        """INSERT INTO turns(game_id, turn_no, status, started_at, deadline,
                             start_board_json)
           VALUES (?, 1, 'open', ?, ?, ?)""",
        (game_id, now, deadline, json.dumps(board)),
    )
    conn.execute(
        "UPDATE games SET status='active', current_turn_no=1, updated_at=? "
        "WHERE game_id=?",
        (now, game_id),
    )
    # 成功开局：双方在本房的报名用掉；其他房的报名继续暂停，直到对局结束
    for seat in seats:
        conn.execute(
            "UPDATE practice_registrations SET status='consumed', updated_at=? "
            "WHERE room_id=? AND account_id=? AND epoch=? AND status='invited'",
            (now, inv["room_id"], seat["account_id"], seat["reg_epoch"]),
        )
    conn.execute(
        """INSERT INTO practice_games(game_id, room_id, invitation_id,
                                      home_account_id, away_account_id, status,
                                      created_at, updated_at)
           VALUES (?,?,?,?,?, 'active', ?, ?)""",
        (
            game_id, inv["room_id"], inv["invitation_id"],
            seats[0]["account_id"], seats[1]["account_id"], now, now,
        ),
    )
    conn.execute(
        "UPDATE practice_invitations SET status='started', game_id=?, updated_at=? "
        "WHERE invitation_id=?",
        (game_id, now, inv["invitation_id"]),
    )
    return game_id


def _confirm_result(
    conn: sqlite3.Connection, invitation_id: str, account_id: str, replay: bool
) -> dict[str, Any]:
    inv = _get_invitation(conn, invitation_id)
    view = _invitation_view(conn, inv, my_account_id=account_id)
    return {
        "invitation_id": invitation_id,
        "room_id": inv["room_id"],
        "status": inv["status"],
        "my_response": view["my_response"] or "confirmed",
        "all_confirmed": all(s["response"] == "confirmed" for s in view["seats"]),
        "started": inv["status"] == "started",
        "game_id": inv["game_id"],
        "idempotent_replay": replay,
        "invitation": view,
    }


def confirm_invitation(invitation_id: str, secret: str) -> dict[str, Any]:
    """凭本人账号确认邀请；第二人确认时直接开出一局可玩的比赛。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        inv = _get_invitation(conn, invitation_id)
        seat = _get_seat(conn, invitation_id, account["account_id"])
        now_dt = _now()
        now = _iso(now_dt)
        if inv["status"] == "pending":
            if inv["deadline"] < now:
                # 过期邀请不能被迟到的确认复活
                _resolve_expired(conn, inv, now)
                raise ServiceError(409, "invitation_expired", "邀请已过期，确认无效")
            if seat["response"] == "confirmed":
                # 本人已确认过的重试：幂等返回，不产生副作用
                return _confirm_result(conn, invitation_id, account["account_id"], True)
            conn.execute(
                "UPDATE practice_invitation_seats SET response='confirmed', "
                "responded_at=? WHERE invitation_id=? AND account_id=?",
                (now, invitation_id, account["account_id"]),
            )
            other = conn.execute(
                "SELECT * FROM practice_invitation_seats "
                "WHERE invitation_id=? AND account_id!=?",
                (invitation_id, account["account_id"]),
            ).fetchone()
            if other is not None and other["response"] == "confirmed":
                # 第二人确认：同一事务内直接开局
                _start_practice_game(conn, inv, now_dt)
            return _confirm_result(conn, invitation_id, account["account_id"], False)
        if inv["status"] == "started":
            # 旧确认的重试：对局已开出，返回原结果，绝不开新局
            return _confirm_result(conn, invitation_id, account["account_id"], True)
        if inv["status"] == "expired":
            raise ServiceError(409, "invitation_expired", "邀请已过期，不能确认")
        raise ServiceError(409, "invitation_closed", "邀请已解除，不能确认")

    return run_transaction(tx)


def _decline_result(
    conn: sqlite3.Connection, invitation_id: str, account_id: str, replay: bool
) -> dict[str, Any]:
    inv = _get_invitation(conn, invitation_id)
    view = _invitation_view(conn, inv, my_account_id=account_id)
    return {
        "invitation_id": invitation_id,
        "room_id": inv["room_id"],
        "status": inv["status"],
        "my_response": view["my_response"] or "declined",
        "idempotent_replay": replay,
        "invitation": view,
    }


def decline_invitation(invitation_id: str, secret: str) -> dict[str, Any]:
    """拒绝邀请：解除邀请，仅拒绝者从该房退队，其余报名恢复原顺序。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        inv = _get_invitation(conn, invitation_id)
        seat = _get_seat(conn, invitation_id, account["account_id"])
        now = _iso(_now())
        if inv["status"] == "pending":
            if inv["deadline"] < now:
                _resolve_expired(conn, inv, now)
                raise ServiceError(409, "invitation_expired", "邀请已过期，按过期处理")
            conn.execute(
                "UPDATE practice_invitation_seats SET response='declined', "
                "responded_at=? WHERE invitation_id=? AND account_id=?",
                (now, invitation_id, account["account_id"]),
            )
            _resolve_declined(conn, inv, account["account_id"], now)
            return _decline_result(conn, invitation_id, account["account_id"], False)
        if inv["status"] == "declined":
            if seat["response"] == "declined":
                # 本人旧拒绝的重试：幂等返回，不影响其他报名
                return _decline_result(conn, invitation_id, account["account_id"], True)
            raise ServiceError(409, "invitation_closed", "邀请已解除")
        if inv["status"] == "expired":
            raise ServiceError(409, "invitation_expired", "邀请已过期")
        raise ServiceError(409, "invitation_closed", "对局已开出，邀请不可再拒绝")

    return run_transaction(tx)


def get_invitation_view(invitation_id: str, secret: str) -> dict[str, Any]:
    """当事人查询邀请当前状态（含过期懒处理）。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        inv = _get_invitation(conn, invitation_id)
        _get_seat(conn, invitation_id, account["account_id"])
        now = _iso(_now())
        if inv["status"] == "pending" and inv["deadline"] < now:
            _resolve_expired(conn, inv, now)
            inv = _get_invitation(conn, invitation_id)
        return _invitation_view(conn, inv, my_account_id=account["account_id"])

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 选手自查：排队状态、待回复邀请、已配成对局
# ---------------------------------------------------------------------------
def _my_lobby_view(conn: sqlite3.Connection, account: sqlite3.Row) -> dict[str, Any]:
    aid = account["account_id"]
    regs = conn.execute(
        """SELECT r.*, p.name AS room_name
           FROM practice_registrations r
           JOIN practice_rooms p ON p.room_id = r.room_id
           WHERE r.account_id=? ORDER BY r.created_at, r.room_id""",
        (aid,),
    ).fetchall()
    reg_views = []
    for r in regs:
        position = None
        if r["status"] == "queued":
            position = conn.execute(
                "SELECT COUNT(*) + 1 AS p FROM practice_registrations "
                "WHERE room_id=? AND status='queued' AND seq<?",
                (r["room_id"], r["seq"]),
            ).fetchone()["p"]
        reg_views.append(
            {
                "room_id": r["room_id"],
                "room_name": r["room_name"],
                "status": r["status"],
                "epoch": r["epoch"],
                "seq": r["seq"],
                "position": position,
                "request_id": r["request_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    invs = conn.execute(
        """SELECT i.* FROM practice_invitations i
           JOIN practice_invitation_seats s ON s.invitation_id = i.invitation_id
           WHERE s.account_id=? AND i.status='pending'
           ORDER BY i.created_at""",
        (aid,),
    ).fetchall()
    games = conn.execute(
        """SELECT * FROM practice_games
           WHERE home_account_id=? OR away_account_id=? ORDER BY created_at""",
        (aid, aid),
    ).fetchall()
    return {
        "account_id": aid,
        "name": account["name"],
        "registrations": reg_views,
        "invitations": [
            _invitation_view(conn, inv, my_account_id=aid) for inv in invs
        ],
        "games": [_practice_game_view(conn, g, my_account_id=aid) for g in games],
    }


def get_my_lobby(secret: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        account = _authenticate_account(conn, secret)
        # 懒处理过期邀请：待回复列表只包含真正仍可回复的邀请
        _expire_overdue(conn, _iso(_now()))
        return _my_lobby_view(conn, account)

    return run_transaction(tx)


def get_my_registrations(secret: str) -> list[dict[str, Any]]:
    return get_my_lobby(secret)["registrations"]


def get_my_invitations(secret: str) -> list[dict[str, Any]]:
    return get_my_lobby(secret)["invitations"]


def get_my_games(secret: str) -> list[dict[str, Any]]:
    return get_my_lobby(secret)["games"]


# ---------------------------------------------------------------------------
# 裁定员提前结束练习（不算完赛；保留公告，不再接手/提交/裁定）
# ---------------------------------------------------------------------------
def end_practice_game(game_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        _get_game(conn, game_id)
        pg = _get_practice_game_row(conn, game_id)
        if pg["status"] == "ended":
            raise ServiceError(409, "practice_already_ended", "该练习赛已提前结束")
        if pg["status"] == "finished":
            raise ServiceError(
                409, "practice_already_finished", "该练习赛已全员完赛，不能提前结束"
            )
        now = _iso(_now())
        conn.execute(
            "UPDATE practice_games SET status='ended', ended_at=?, updated_at=? "
            "WHERE game_id=?",
            (now, now, game_id),
        )
        released = []
        for account_id in (pg["home_account_id"], pg["away_account_id"]):
            # 双方在大厅其他房间的暂停报名恢复原顺序
            _resume_account_registrations(conn, account_id, now)
            released.append(account_id)
        return {
            "game_id": game_id,
            "room_id": pg["room_id"],
            "status": "ended",
            "ended_at": now,
            "released_accounts": released,
        }

    return run_transaction(tx)


def get_practice_game_view(game_id: str) -> dict[str, Any]:
    """公开查询练习对局归属（房间、邀请、状态）。"""

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        _get_game(conn, game_id)
        pg = _get_practice_game_row(conn, game_id)
        return _practice_game_view(conn, pg)

    return run_transaction(tx)
