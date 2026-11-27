"""业务服务层：所有状态变更都在 BEGIN IMMEDIATE 事务中完成。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import engine
from .db import get_pepper, get_conn, run_transaction
from .security import hash_secret, new_token, verify_secret


class ServiceError(Exception):
    def __init__(self, status_code: int, error: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _loads(raw: str | None) -> Any:
    return json.loads(raw) if raw is not None else None


def _blocked_set(game: sqlite3.Row) -> set[tuple[int, int]]:
    return {tuple(c) for c in _loads(game["blocked_json"])}  # type: ignore[misc]


def _finish_set(game: sqlite3.Row) -> set[tuple[int, int]]:
    return {tuple(c) for c in _loads(game["finish_json"])}  # type: ignore[misc]


def _get_game(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "game_not_found", f"游戏 {game_id} 不存在")
    return row


def _get_player(conn: sqlite3.Connection, game_id: str, player_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM players WHERE player_id=? AND game_id=?", (player_id, game_id)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "player_not_found", "该游戏中不存在此玩家")
    return row


def _get_current_turn(conn: sqlite3.Connection, game: sqlite3.Row) -> sqlite3.Row | None:
    if game["current_turn_no"] == 0:
        return None
    return conn.execute(
        "SELECT * FROM turns WHERE game_id=? AND turn_no=?",
        (game["game_id"], game["current_turn_no"]),
    ).fetchone()


def _get_stakes(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    """积分赛扩展信息；普通比赛返回 None。"""
    return conn.execute(
        "SELECT * FROM stakes_games WHERE game_id=?", (game_id,)
    ).fetchone()


def _validate_map(
    width: int, height: int, blocked: list[list[int]], finish: list[list[int]]
) -> None:
    blocked_set = {tuple(c) for c in blocked}
    finish_set = {tuple(c) for c in finish}
    for label, cells in (("blocked", blocked_set), ("finish", finish_set)):
        for x, y in cells:
            if not (0 <= x < width and 0 <= y < height):
                raise ServiceError(422, "cell_out_of_bounds", f"{label} 格 ({x},{y}) 越界")
    if not finish_set:
        raise ServiceError(422, "finish_required", "竞速游戏至少需要一个终点格")
    if blocked_set & finish_set:
        raise ServiceError(422, "cell_overlap", "阻挡格与终点格不可重叠")


# ---------------------------------------------------------------------------
# 积分账本：所有变动与业务状态转换同处一个 BEGIN IMMEDIATE 事务，
# request_key 全局唯一作为幂等兜底，重试/重启不会重复入账。
# ---------------------------------------------------------------------------
def _credit_points(
    conn: sqlite3.Connection,
    account_id: str,
    amount: int,
    game_id: str | None,
    kind: str,
    request_key: str,
    note: str | None,
    now: str,
) -> int:
    """入账（正金额），返回入账后的可用积分。"""
    row = conn.execute(
        "SELECT balance FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "account_not_found", f"账号 {account_id} 不存在")
    new_balance = row["balance"] + amount
    conn.execute(
        "UPDATE accounts SET balance=? WHERE account_id=?", (new_balance, account_id)
    )
    conn.execute(
        """INSERT INTO point_ledger(account_id, game_id, kind, amount,
                                    balance_after, request_key, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (account_id, game_id, kind, amount, new_balance, request_key, note, now),
    )
    return new_balance


def _debit_points(
    conn: sqlite3.Connection,
    account_id: str,
    amount: int,
    game_id: str | None,
    kind: str,
    request_key: str,
    note: str | None,
    now: str,
) -> int:
    """出账（占用），可用积分不足时整体失败、不改变任何状态。"""
    row = conn.execute(
        "SELECT balance FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "account_not_found", f"账号 {account_id} 不存在")
    if row["balance"] < amount:
        raise ServiceError(
            409,
            "insufficient_points",
            f"可用积分 {row['balance']} 不足以支付 {amount}，报名与积分均未改变",
        )
    new_balance = row["balance"] - amount
    conn.execute(
        "UPDATE accounts SET balance=? WHERE account_id=?", (new_balance, account_id)
    )
    conn.execute(
        """INSERT INTO point_ledger(account_id, game_id, kind, amount,
                                    balance_after, request_key, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (account_id, game_id, kind, -amount, new_balance, request_key, note, now),
    )
    return new_balance


def _settle_stakes(
    conn: sqlite3.Connection, game_id: str, stakes: sqlite3.Row, now: str
) -> dict[str, Any]:
    """积分赛全员完赛时的结算（在关回合事务内调用，整局至多执行一次）。

    最早到达终点（finished_turn 最小）的同回合选手平分全部报名费；
    除不尽的余点按账号 ID 升序依次各给一点。
    """
    fee = stakes["entry_fee"]
    enrolled = conn.execute(
        "SELECT account_id FROM enrollments WHERE game_id=? AND status='enrolled'",
        (game_id,),
    ).fetchall()
    pot = fee * len(enrolled)

    finished = conn.execute(
        "SELECT player_id, finished_turn FROM players WHERE game_id=? AND finished=1",
        (game_id,),
    ).fetchall()
    min_turn = min(r["finished_turn"] for r in finished)
    winner_pids = {r["player_id"] for r in finished if r["finished_turn"] == min_turn}

    roster_rows = conn.execute(
        "SELECT player_id, account_id FROM stakes_roster WHERE game_id=?", (game_id,)
    ).fetchall()
    pid_to_acc = {r["player_id"]: r["account_id"] for r in roster_rows}
    acc_to_pid = {r["account_id"]: r["player_id"] for r in roster_rows}
    # 账号 ID 升序：余点依此顺序各给一点
    winner_accounts = sorted(pid_to_acc[pid] for pid in winner_pids)

    share, remainder = divmod(pot, len(winner_accounts))
    payouts: list[dict[str, Any]] = []
    for i, account_id in enumerate(winner_accounts):
        amount = share + (1 if i < remainder else 0)
        _credit_points(
            conn, account_id, amount, game_id, "payout",
            request_key=f"payout:{game_id}:{account_id}",
            note="积分赛奖金", now=now,
        )
        payouts.append(
            {"account_id": account_id, "player_id": acc_to_pid[account_id], "amount": amount}
        )

    conn.execute(
        "UPDATE enrollments SET status='settled', closed_at=? "
        "WHERE game_id=? AND status='enrolled'",
        (now, game_id),
    )
    settlement = {
        "pot": pot,
        "entry_fee": fee,
        "winners": winner_accounts,
        "payouts": payouts,
    }
    conn.execute(
        "UPDATE stakes_games SET status='finished', settlement_json=?, updated_at=? "
        "WHERE game_id=?",
        (json.dumps(settlement), now, game_id),
    )
    return settlement


def _latest_revisions(
    conn: sqlite3.Connection, game_id: str, turn_no: int
) -> dict[str, sqlite3.Row]:
    """每玩家取 sequence 最大的一条（即当前生效的指令版本）。"""
    rows = conn.execute(
        """
        SELECT r.* FROM command_revisions r
        JOIN (
            SELECT player_id, MAX(sequence) AS max_seq
            FROM command_revisions
            WHERE game_id=? AND turn_no=?
            GROUP BY player_id
        ) m ON m.player_id = r.player_id AND m.max_seq = r.sequence
        WHERE r.game_id=? AND r.turn_no=?
        """,
        (game_id, turn_no, game_id, turn_no),
    ).fetchall()
    return {row["player_id"]: row for row in rows}


def _player_public(row: sqlite3.Row) -> dict[str, Any]:
    position = [row["x"], row["y"]] if row["x"] is not None else None
    return {
        "player_id": row["player_id"],
        "name": row["name"],
        "position": position,
        "finished": bool(row["finished"]),
        "finished_turn": row["finished_turn"],
    }


# ---------------------------------------------------------------------------
# 裁定员：建局 / 列表 / 开局 / 查看指令 / 关回合
# ---------------------------------------------------------------------------
def create_game(req: Any) -> dict[str, Any]:
    # 参数一致性校验
    if len(set(req.players)) != len(req.players):
        raise ServiceError(422, "duplicate_player_name", "玩家名不可重复")
    _validate_map(req.width, req.height, req.blocked, req.finish)

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        pepper = get_pepper(conn)
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
        credentials: list[dict[str, Any]] = []
        for name in req.players:
            player_id = new_token(12)
            secret = new_token(24)
            token = new_token(24)
            gen = 1
            conn.execute(
                """INSERT INTO players(player_id, game_id, name, secret_hash,
                                       device_generation, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (player_id, game_id, name, hash_secret(pepper, secret), gen, now),
            )
            conn.execute(
                """INSERT INTO devices(device_id, game_id, player_id, generation, created_at)
                   VALUES (?,?,?,?,?)""",
                (hash_secret(pepper, token), game_id, player_id, gen, now),
            )
            credentials.append(
                {
                    "player_id": player_id,
                    "name": name,
                    "takeover_secret": secret,
                    "device_token": token,
                    "device_generation": gen,
                }
            )
        return {
            "game_id": game_id,
            "status": "setup",
            "current_turn_no": 0,
            "width": req.width,
            "height": req.height,
            "blocked": req.blocked,
            "finish": req.finish,
            "turn_duration_seconds": req.turn_duration_seconds,
            "players": credentials,
        }

    return run_transaction(tx)


def list_games() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM games ORDER BY created_at").fetchall()
        return [
            {
                "game_id": r["game_id"],
                "name": r["name"],
                # 已取消的积分赛对外呈现 cancelled
                "status": _effective_status(conn, r),
                "current_turn_no": r["current_turn_no"],
                "width": r["width"],
                "height": r["height"],
                "blocked": _loads(r["blocked_json"]),
                "finish": _loads(r["finish_json"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def _effective_status(conn: sqlite3.Connection, game: sqlite3.Row) -> str:
    stakes = _get_stakes(conn, game["game_id"])
    if stakes is not None and stakes["status"] == "cancelled":
        return "cancelled"
    return game["status"]


def start_game(game_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        game = _get_game(conn, game_id)
        if game["status"] != "setup":
            raise ServiceError(409, "game_not_in_setup", "仅 setup 状态的游戏可以开局")
        stakes = _get_stakes(conn, game_id)
        if stakes is not None:
            if stakes["status"] == "cancelled":
                raise ServiceError(409, "stakes_cancelled", "该积分赛已取消，不能发车")
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM stakes_roster WHERE game_id=?) AS roster, "
                "(SELECT COUNT(*) FROM enrollments WHERE game_id=? AND status='enrolled') "
                "AS enrolled",
                (game_id, game_id),
            ).fetchone()
            if counts["enrolled"] < counts["roster"]:
                raise ServiceError(
                    409,
                    "roster_not_fully_enrolled",
                    f"名单 {counts['roster']} 人中仅 {counts['enrolled']} 人报名，"
                    "名单上的选手全部报名后才可发车",
                )
        players = conn.execute(
            "SELECT * FROM players WHERE game_id=? ORDER BY rowid", (game_id,)
        ).fetchall()
        blocked = _blocked_set(game)
        finish = _finish_set(game)

        # 起点：左边界上自上而下的可通行格
        starts = [
            (0, y)
            for y in range(game["height"])
            if (0, y) not in blocked and (0, y) not in finish
        ]
        if len(starts) < len(players):
            raise ServiceError(
                409,
                "not_enough_start_cells",
                f"左边界可通行起点 {len(starts)} 个，少于玩家数 {len(players)}",
            )

        now_dt = _now()
        now = _iso(now_dt)
        board: dict[str, list[int]] = {}
        for player, (x, y) in zip(players, starts):
            conn.execute(
                "UPDATE players SET x=?, y=? WHERE player_id=?", (x, y, player["player_id"])
            )
            board[player["player_id"]] = [x, y]

        deadline = _iso(now_dt + timedelta(seconds=game["turn_duration_seconds"]))
        conn.execute(
            """INSERT INTO turns(game_id, turn_no, status, started_at, deadline,
                                 start_board_json)
               VALUES (?, 1, 'open', ?, ?, ?)""",
            (game_id, now, deadline, json.dumps(board)),
        )
        conn.execute(
            "UPDATE games SET status='active', current_turn_no=1, updated_at=? WHERE game_id=?",
            (now, game_id),
        )
        if stakes is not None:
            conn.execute(
                "UPDATE stakes_games SET status='active', updated_at=? WHERE game_id=?",
                (now, game_id),
            )
        return {"game_id": game_id, "status": "active", "current_turn_no": 1, "deadline": deadline}

    return run_transaction(tx)


def referee_turn_detail(game_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        game = _get_game(conn, game_id)
        turn = _get_current_turn(conn, game)
        if turn is None:
            raise ServiceError(409, "no_turn", "游戏尚未开局")
        players = conn.execute(
            "SELECT player_id, name FROM players WHERE game_id=? ORDER BY rowid", (game_id,)
        ).fetchall()
        latest = _latest_revisions(conn, game_id, turn["turn_no"])
        commands = []
        for p in players:
            rev = latest.get(p["player_id"])
            commands.append(
                {
                    "player_id": p["player_id"],
                    "name": p["name"],
                    "direction": rev["direction"] if rev else None,
                    "sequence": rev["sequence"] if rev else None,
                    "device_generation": rev["device_generation"] if rev else None,
                }
            )
        return {
            "game_id": game_id,
            "turn_no": turn["turn_no"],
            "status": turn["status"],
            "started_at": turn["started_at"],
            "deadline": turn["deadline"],
            "closed_at": turn["closed_at"],
            "commands": commands,
        }
    finally:
        conn.close()


def close_turn(game_id: str, expected_turn_no: int | None = None) -> dict[str, Any]:
    """关回合：裁定 + 公告 + 开下一回合，整体为一个原子事务。

    每个回合的裁定与公开严格只发生一次。若调用方带 ``expected_turn_no``，
    而当前回合已不是该回合（典型场景：并发重复关回合），返回 409 而不是
    误关新回合；新回合在同一事务提交前对外不可见、不可提交。
    """

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        game = _get_game(conn, game_id)
        stakes = _get_stakes(conn, game_id)
        if stakes is not None and stakes["status"] == "cancelled":
            # 取消与最终裁定并发时，写锁保证只有一个终局生效
            raise ServiceError(409, "stakes_cancelled", "该积分赛已取消，不再裁定新结果")
        if game["status"] != "active":
            if game["status"] == "finished":
                raise ServiceError(409, "game_finished", "游戏已结束，没有可关闭的回合")
            raise ServiceError(409, "game_not_active", "游戏未在进行中，无法关回合")
        turn = _get_current_turn(conn, game)
        if turn is None:
            raise ServiceError(409, "no_open_turn", "当前没有可关闭的回合")

        if expected_turn_no is not None and expected_turn_no != turn["turn_no"]:
            prev = conn.execute(
                "SELECT status FROM turns WHERE game_id=? AND turn_no=?",
                (game_id, expected_turn_no),
            ).fetchone()
            if prev is not None and prev["status"] == "closed":
                raise ServiceError(
                    409,
                    "turn_already_closed",
                    f"回合 {expected_turn_no} 已裁定并公告，当前可玩回合为 {turn['turn_no']}",
                )
            raise ServiceError(
                409,
                "turn_conflict",
                f"期望关闭回合 {expected_turn_no}，但当前回合为 {turn['turn_no']}",
            )

        turn_no = turn["turn_no"]
        players = conn.execute(
            "SELECT * FROM players WHERE game_id=? ORDER BY rowid", (game_id,)
        ).fetchall()

        # 以回合开始时的棋盘为准
        start_board: dict[str, tuple[int, int]] = {
            pid: tuple(pos) for pid, pos in _loads(turn["start_board_json"]).items()  # type: ignore[misc]
        }
        latest = _latest_revisions(conn, game_id, turn_no)
        # 公告对应的“确实存在的已接受指令集合”：每个玩家其最后一个被接受的版本
        commands = {pid: row["direction"] for pid, row in latest.items()}

        blocked = _blocked_set(game)
        results = engine.adjudicate(
            start_board, commands, blocked, game["width"], game["height"]
        )

        finish = _finish_set(game)
        now_dt = _now()
        now = _iso(now_dt)
        new_board: dict[str, list[int]] = {}
        serialized_results: list[dict[str, Any]] = []
        newly_finished: list[str] = []

        for p in players:
            pid = p["player_id"]
            r = results[pid]
            if bool(p["finished"]):
                # 已完赛者留在终点格，不参与本回合移动
                r["reason"] = "already_finished"
                r["success"] = True
                r["x"], r["y"] = start_board[pid]
                r["direction"] = commands.get(pid, "wait")
            just_finished = False
            if not bool(p["finished"]) and (r["x"], r["y"]) in finish:
                just_finished = True
                newly_finished.append(pid)
            conn.execute(
                "UPDATE players SET x=?, y=?, finished=?, finished_turn=COALESCE(finished_turn, ?) "
                "WHERE player_id=?",
                (
                    r["x"], r["y"],
                    1 if (p["finished"] or just_finished) else 0,
                    turn_no if just_finished else None,
                    pid,
                ),
            )
            new_board[pid] = [r["x"], r["y"]]
            serialized_results.append(
                {
                    "player_id": pid,
                    "x": r["x"],
                    "y": r["y"],
                    "direction": r["direction"],
                    "success": r["success"],
                    "reason": r["reason"],
                    "finished": bool(p["finished"]) or just_finished,
                }
            )

        # 名次：先完赛在前，同回合并列按 player_id 排序
        winners = [
            r["player_id"]
            for r in conn.execute(
                """SELECT player_id FROM players
                   WHERE game_id=? AND finished=1
                   ORDER BY finished_turn, player_id""",
                (game_id,),
            ).fetchall()
        ]

        all_finished = all(bool(p["finished"]) or p["player_id"] in newly_finished for p in players)
        new_status = "finished" if all_finished else "active"

        # 积分赛：全员完赛才结算；此前即使已有选手到达终点也不发奖
        settlement = None
        if all_finished and stakes is not None:
            settlement = _settle_stakes(conn, game_id, stakes, now)

        announcement = {
            "game_id": game_id,
            "turn_no": turn_no,
            "closed_at": now,
            "deadline": turn["deadline"],
            "board": new_board,
            "results": serialized_results,
            "winners": winners,
            "game_status": new_status,
        }
        if settlement is not None:
            announcement["stakes"] = settlement

        conn.execute(
            """UPDATE turns SET status='closed', closed_at=?, announcement_json=?
               WHERE game_id=? AND turn_no=?""",
            (now, json.dumps(announcement), game_id, turn_no),
        )

        if all_finished:
            conn.execute(
                "UPDATE games SET status='finished', updated_at=? WHERE game_id=?",
                (now, game_id),
            )
        else:
            next_deadline = _iso(now_dt + timedelta(seconds=game["turn_duration_seconds"]))
            # 新回合公开可玩：同一事务内写入，未提交前任何设备都无法看到/提交
            conn.execute(
                """INSERT INTO turns(game_id, turn_no, status, started_at, deadline,
                                     start_board_json)
                   VALUES (?, ?, 'open', ?, ?, ?)""",
                (game_id, turn_no + 1, now, next_deadline, json.dumps(new_board)),
            )
            conn.execute(
                "UPDATE games SET current_turn_no=?, updated_at=? WHERE game_id=?",
                (turn_no + 1, now, game_id),
            )

        return {"idempotent": False, "announcement": announcement}

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 设备：接手 / 查看自身状态 / 提交与替换指令
# ---------------------------------------------------------------------------
def takeover(game_id: str, player_id: str, secret: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        pepper = get_pepper(conn)
        game = _get_game(conn, game_id)
        player = _get_player(conn, game_id, player_id)
        stakes = _get_stakes(conn, game_id)
        if stakes is not None and stakes["status"] == "cancelled":
            raise ServiceError(409, "stakes_cancelled", "该积分赛已取消，不能再接手")
        if not verify_secret(pepper, secret, player["secret_hash"]):
            raise ServiceError(401, "bad_secret", "接手密钥错误")
        if stakes is not None:
            # 积分赛席位：仅已报名（或已随完赛结算）的账号可凭本人账号密钥接手
            enr = conn.execute(
                "SELECT status FROM enrollments WHERE game_id=? AND account_id=?",
                (game_id, player["account_id"]),
            ).fetchone()
            if enr is None or enr["status"] not in ("enrolled", "settled"):
                raise ServiceError(
                    409, "not_enrolled", "该席位对应的账号未报名本场比赛，不能接手"
                )

        old_generation = player["device_generation"]
        new_generation = old_generation + 1
        now = _iso(_now())

        # 旧设备立即失效（revoked），其写请求将被拒绝
        conn.execute(
            "UPDATE devices SET revoked_at=? WHERE player_id=? AND revoked_at IS NULL",
            (now, player_id),
        )
        token = new_token(24)
        conn.execute(
            """INSERT INTO devices(device_id, game_id, player_id, generation, created_at)
               VALUES (?,?,?,?,?)""",
            (hash_secret(pepper, token), game_id, player_id, new_generation, now),
        )
        conn.execute(
            "UPDATE players SET device_generation=? WHERE player_id=?",
            (new_generation, player_id),
        )

        # 此前已被接受的指令保留；新设备可查到当前生效版本
        turn = _get_current_turn(conn, game)
        accepted_direction = None
        can_replace = False
        if turn is not None and turn["status"] == "open":
            latest = _latest_revisions(conn, game_id, turn["turn_no"])
            rev = latest.get(player_id)
            if rev is not None:
                accepted_direction = rev["direction"]
            can_replace = not bool(player["finished"])

        return {
            "game_id": game_id,
            "player_id": player_id,
            "device_token": token,
            "device_generation": new_generation,
            "old_generation": old_generation,
            "current_turn_no": game["current_turn_no"],
            "turn_status": turn["status"] if turn else "no_turn",
            "accepted_direction": accepted_direction,
            "can_replace": can_replace,
        }

    return run_transaction(tx)


def _authenticate_device(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    pepper = get_pepper(conn)
    row = conn.execute(
        "SELECT * FROM devices WHERE device_id=?", (hash_secret(pepper, token),)
    ).fetchone()
    if row is None:
        raise ServiceError(401, "bad_device_token", "设备令牌无效")
    if row["revoked_at"] is not None:
        # 已被接手取代：明确告知设备已失效，而非笼统的未授权
        raise ServiceError(409, "device_superseded", "该设备已被新设备接手，无法再执行写操作")
    player = conn.execute(
        "SELECT * FROM players WHERE player_id=?", (row["player_id"],)
    ).fetchone()
    if player["device_generation"] != row["generation"]:
        raise ServiceError(409, "device_superseded", "该设备已被新设备接手，无法再执行写操作")
    return row


def _my_command_state(
    conn: sqlite3.Connection, game: sqlite3.Row, player: sqlite3.Row
) -> tuple[str | None, bool, str]:
    """返回 (当前生效指令, 是否可替换, 不可替换原因)。"""
    turn = _get_current_turn(conn, game)
    if turn is None:
        return None, False, "no_open_turn"
    if turn["status"] != "open":
        # 关回合与下一回合公开之间：任何设备都不能提交
        return None, False, "turn_closed"
    latest = _latest_revisions(conn, game["game_id"], turn["turn_no"])
    rev = latest.get(player["player_id"])
    direction = rev["direction"] if rev else None
    if bool(player["finished"]):
        return direction, False, "player_finished"
    return direction, True, "open"


def get_my_state(game_id: str, token: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        device = _authenticate_device(conn, token)
        if device["game_id"] != game_id:
            raise ServiceError(404, "game_not_found", "设备不属于该游戏")
        game = _get_game(conn, game_id)
        player = _get_player(conn, game_id, device["player_id"])
        direction, can_replace, reason = _my_command_state(conn, game, player)
        turn = _get_current_turn(conn, game)
        return {
            "player_id": player["player_id"],
            "name": player["name"],
            "position": [player["x"], player["y"]] if player["x"] is not None else None,
            "finished": bool(player["finished"]),
            "device_generation": player["device_generation"],
            "current_turn_no": game["current_turn_no"],
            "turn_status": turn["status"] if turn else "no_turn",
            "accepted_direction": direction,
            "can_replace": can_replace,
            "replaceable_reason": reason,
        }
    finally:
        conn.close()


def submit_command(game_id: str, token: str, direction: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        device = _authenticate_device(conn, token)
        if device["game_id"] != game_id:
            raise ServiceError(404, "game_not_found", "设备不属于该游戏")
        game = _get_game(conn, game_id)
        player = _get_player(conn, game_id, device["player_id"])

        stakes = _get_stakes(conn, game_id)
        if stakes is not None and stakes["status"] == "cancelled":
            raise ServiceError(409, "stakes_cancelled", "该积分赛已取消，不能提交指令")
        if game["status"] != "active":
            raise ServiceError(409, "game_not_active", "游戏未在进行中，不能提交指令")
        turn = _get_current_turn(conn, game)
        if turn is None or turn["status"] != "open":
            # 覆盖“关回合后、新回合公开前”的窗口
            raise ServiceError(409, "turn_not_open", "当前回合不可提交指令")
        if bool(player["finished"]):
            raise ServiceError(409, "player_finished", "该玩家已完赛，不能再提交指令")

        # 提交时即校验合法性（相邻可通行格；wait 恒合法）
        x, y = player["x"], player["y"]
        if direction != "wait":
            tx_, ty_ = engine.neighbor(x, y, direction)
            if not (0 <= tx_ < game["width"] and 0 <= ty_ < game["height"]):
                raise ServiceError(422, "move_out_of_bounds", "移动目标超出棋盘")
            if (tx_, ty_) in _blocked_set(game):
                raise ServiceError(422, "move_blocked", "移动目标为阻挡格")

        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM command_revisions "
            "WHERE game_id=? AND turn_no=? AND player_id=?",
            (game_id, turn["turn_no"], player["player_id"]),
        ).fetchone()
        sequence = row["s"] + 1
        revision_id = new_token(16)
        conn.execute(
            """INSERT INTO command_revisions(revision_id, game_id, turn_no, player_id,
                                             direction, sequence, device_generation, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                revision_id, game_id, turn["turn_no"], player["player_id"],
                direction, sequence, device["generation"], _iso(_now()),
            ),
        )
        return {
            "game_id": game_id,
            "turn_no": turn["turn_no"],
            "direction": direction,
            "sequence": sequence,
            "device_generation": device["generation"],
            "replaceable": True,
        }

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 公开只读视图（任何人可看，不含未公开指令/提交进度）
# ---------------------------------------------------------------------------
def get_announcement(game_id: str, turn_no: int) -> dict[str, Any]:
    conn = get_conn()
    try:
        _get_game(conn, game_id)
        row = conn.execute(
            "SELECT announcement_json FROM turns WHERE game_id=? AND turn_no=?",
            (game_id, turn_no),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "turn_not_found", f"回合 {turn_no} 不存在")
        if row["announcement_json"] is None:
            raise ServiceError(409, "turn_not_closed", f"回合 {turn_no} 尚未关闭")
        return _loads(row["announcement_json"])
    finally:
        conn.close()


def get_public_state(game_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        game = _get_game(conn, game_id)
        players = conn.execute(
            "SELECT * FROM players WHERE game_id=? ORDER BY rowid", (game_id,)
        ).fetchall()
        turn = _get_current_turn(conn, game)

        # 棋盘只反映已公告状态：玩家位置随关回合事务更新，未关回合时不变
        board = {
            p["player_id"]: [p["x"], p["y"]]
            for p in players
            if p["x"] is not None
        }
        winners_rows = conn.execute(
            "SELECT player_id FROM players WHERE game_id=? AND finished=1 "
            "ORDER BY finished_turn, player_id",
            (game_id,),
        ).fetchall()
        stakes_summary = None
        stakes = _get_stakes(conn, game_id)
        if stakes is not None:
            enrolled = conn.execute(
                "SELECT COUNT(*) AS c FROM enrollments WHERE game_id=? AND status='enrolled'",
                (game_id,),
            ).fetchone()["c"]
            roster_size = conn.execute(
                "SELECT COUNT(*) AS c FROM stakes_roster WHERE game_id=?", (game_id,)
            ).fetchone()["c"]
            stakes_summary = {
                "status": stakes["status"],
                "entry_fee": stakes["entry_fee"],
                "enrolled": enrolled,
                "roster_size": roster_size,
                "pot": stakes["entry_fee"] * enrolled,
            }
        return {
            "game_id": game_id,
            "name": game["name"],
            "status": _effective_status(conn, game),
            "width": game["width"],
            "height": game["height"],
            "blocked": _loads(game["blocked_json"]),
            "finish": _loads(game["finish_json"]),
            "current_turn_no": game["current_turn_no"],
            "turn": (
                {
                    "turn_no": turn["turn_no"],
                    "status": turn["status"],
                    "started_at": turn["started_at"],
                    "deadline": turn["deadline"],
                    "closed_at": turn["closed_at"],
                }
                if turn
                else None
            ),
            "board": board,
            "players": [_player_public(p) for p in players],
            "winners": [r["player_id"] for r in winners_rows],
            "stakes": stakes_summary,
        }
    finally:
        conn.close()
