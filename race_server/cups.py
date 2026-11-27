"""八人双败杯业务逻辑。

设计要点
--------
- **签表是结构，结果是派生**：创建时即固定 1..8 签位与全部场次（首轮相邻配对
  ``(1,2)(3,4)(5,6)(7,8)``，重赛不重新抽签）。每个场次的结果只来自它当前
  **有效**的已完赛对局；失效对局从结果集合中消失，其下游场次的对局随之失效，
  因此冠军、可开赛场次等全部由有效对局重新推导，不存在“凭失效资格继续比赛”。
- **每个场次至多一局有效**：开赛前检查该场次是否已有有效对局，检查与写入同在
  ���个 ``BEGIN IMMEDIATE`` 事务中，单写者串行化保证不会出现两局同时有效。
- **对局即普通比赛**：签表场次开赛时创建一条普通 ``games`` 记录与两名席位选手
  （席位密钥即账号密钥），沿用既有接手/提交/关回合/公告接口与回合玩法。
- **幂等与迟到请求**：开赛/重赛按 ``request_id`` 幂等；请求记录持久化在
  ``cup_requests``，服务重启后重放仍返回首次结果。开赛还可带 ``expected_revision``，
  迟到请求即使换了 ``request_id`` 也会因该场次已有有效对局而被拒绝，无法操作新对局。
- **重赛**：仅对已完赛且有效的对局生效；该局及其下游所有有效对局（无论进行中
  还是已完赛）一并失效，已公开公告保留，但失效对局拒绝接手/提交/裁定。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from . import cup_bracket as bracket
from .db import get_conn, run_transaction
from .security import new_token
from .services import (
    ServiceError,
    _get_game,
    _iso,
    _loads,
    _now,
    _validate_map,
)


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------
def _get_cup(conn: sqlite3.Connection, cup_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cups WHERE cup_id=?", (cup_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "cup_not_found", f"杯赛 {cup_id} 不存在")
    return row


def _seed_rows(conn: sqlite3.Connection, cup_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cup_seeds WHERE cup_id=? ORDER BY seed_no", (cup_id,)
    ).fetchall()


def _all_cup_games(conn: sqlite3.Connection, cup_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cup_games WHERE cup_id=? ORDER BY match_no, order_index",
        (cup_id,),
    ).fetchall()


def _game_player_map(
    conn: sqlite3.Connection, cup_id: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """game_id -> player_id -> {account_id, seed_no}。"""
    rows = conn.execute(
        """SELECT p.game_id, p.player_id, p.account_id, s.seed_no
           FROM players p
           JOIN cup_games cg ON cg.game_id = p.game_id
           LEFT JOIN cup_seeds s
             ON s.cup_id = cg.cup_id AND s.account_id = p.account_id
           WHERE cg.cup_id=?""",
        (cup_id,),
    ).fetchall()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["game_id"], {})[r["player_id"]] = {
            "account_id": r["account_id"],
            "seed_no": r["seed_no"],
        }
    return out


def _valid_results(
    conn: sqlite3.Connection,
    cup_id: str,
    games: list[sqlite3.Row] | None = None,
    player_map: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[int, tuple[int, int] | None]:
    """当前有效签表下的场次结果 {match_no: (winner_seed, loser_seed)}。"""
    if games is None:
        games = _all_cup_games(conn, cup_id)
    if player_map is None:
        player_map = _game_player_map(conn, cup_id)
    results: dict[int, tuple[int, int] | None] = {}
    for g in games:
        if not g["valid"] or g["winner_player_id"] is None:
            continue
        seats = player_map.get(g["game_id"], {})
        w = seats.get(g["winner_player_id"], {}).get("seed_no")
        lrow = seats.get(g["loser_player_id"]) if g["loser_player_id"] else None
        l = lrow.get("seed_no") if lrow else None
        if w is None:
            continue
        # 防御：同一场次不应有第二局有效结果
        if g["match_no"] in results:
            raise RuntimeError(f"场次 {g['match_no']} 存在多个有效结果")
        results[g["match_no"]] = (w, l)
    return results


# ---------------------------------------------------------------------------
# 创建杯赛：固定签位
# ---------------------------------------------------------------------------
def _cup_map_capacity(
    width: int, height: int, blocked: list[list[int]], finish: list[list[int]]
) -> tuple[int, int]:
    blocked_set = {tuple(c) for c in blocked}
    finish_set = {tuple(c) for c in finish}
    starts = [
        (0, y) for y in range(height)
        if (0, y) not in blocked_set and (0, y) not in finish_set
    ]
    return len(starts), len(finish_set)


def create_cup(req: Any) -> dict[str, Any]:
    if len(set(req.seeds)) != len(req.seeds):
        raise ServiceError(422, "duplicate_seed_account", "八个签位必须是不同账号")
    if len(req.seeds) != 8:
        raise ServiceError(422, "need_eight_seeds", "杯赛必须恰好有八个签位")
    _validate_map(req.width, req.height, req.blocked, req.finish)
    starts, finish_cells = _cup_map_capacity(req.width, req.height, req.blocked, req.finish)
    if starts < 2:
        raise ServiceError(
            422, "not_enough_start_cells",
            "左边界至少需要 2 个可通行起点格供两名选手入场",
        )
    if finish_cells < 2:
        raise ServiceError(
            422, "not_enough_finish_cells",
            "两名选手都须完赛，终点格至少需要 2 个",
        )

    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        accounts: dict[str, sqlite3.Row] = {}
        for name in req.seeds:
            row = conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ServiceError(422, "account_not_found", f"账号 {name} 不存在")
            accounts[name] = row
        cup_id = new_token(12)
        now = _iso(_now())
        conn.execute(
            """INSERT INTO cups(cup_id, name, width, height, blocked_json, finish_json,
                                turn_duration_seconds, revision, champion_withdrawn,
                                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 0, 0, ?, ?)""",
            (
                cup_id, req.name, req.width, req.height,
                json.dumps(req.blocked), json.dumps(req.finish),
                req.turn_duration_seconds, now, now,
            ),
        )
        for seed_no, name in enumerate(req.seeds, start=1):
            account = accounts[name]
            conn.execute(
                "INSERT INTO cup_seeds(cup_id, seed_no, account_id, name) VALUES (?,?,?,?)",
                (cup_id, seed_no, account["account_id"], account["name"]),
            )
        state = _build_state(conn, _get_cup(conn, cup_id))
        first_round = [m for m in state["matches"] if m["match_no"] in (1, 2, 3, 4)]
        return {
            "cup_id": cup_id,
            "status": state["status"],
            "revision": state["revision"],
            "width": req.width,
            "height": req.height,
            "blocked": req.blocked,
            "finish": req.finish,
            "turn_duration_seconds": req.turn_duration_seconds,
            "seeds": state["seeds"],
            "first_round": first_round,
        }

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 开赛：为某场次创建一局普通比赛
# ---------------------------------------------------------------------------
def _create_and_open_game(
    conn: sqlite3.Connection,
    cup: sqlite3.Row,
    match_no: int,
    home_seed: int,
    away_seed: int,
) -> str:
    cup_id = cup["cup_id"]
    seed_rows = {
        r["seed_no"]: r for r in _seed_rows(conn, cup_id)
    }
    game_id = new_token(12)
    now_dt = _now()
    now = _iso(now_dt)
    conn.execute(
        """INSERT INTO games(game_id, name, width, height, blocked_json, finish_json,
                             turn_duration_seconds, status, current_turn_no,
                             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?, 'setup', 0, ?, ?)""",
        (
            game_id, f"{cup['name']} M{match_no}", cup["width"], cup["height"],
            cup["blocked_json"], cup["finish_json"], cup["turn_duration_seconds"],
            now, now,
        ),
    )
    for seed_no in (home_seed, away_seed):
        seed = seed_rows[seed_no]
        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id=?", (seed["account_id"],)
        ).fetchone()
        # 席位密钥即账号密钥：选手凭本人账号接手本局对应席位
        conn.execute(
            """INSERT INTO players(player_id, game_id, name, secret_hash,
                                   device_generation, account_id, created_at)
               VALUES (?,?,?,?, 0, ?, ?)""",
            (
                new_token(12), game_id, account["name"], account["secret_hash"],
                account["account_id"], now,
            ),
        )
    order_index = conn.execute(
        "SELECT COALESCE(MAX(order_index), 0) + 1 AS n FROM cup_games "
        "WHERE cup_id=? AND match_no=?",
        (cup_id, match_no),
    ).fetchone()["n"]
    slot = "decider" if match_no == bracket.DECIDER_MATCH else "regular"
    conn.execute(
        """INSERT INTO cup_games(game_id, cup_id, match_no, order_index, slot,
                                 home_seed, away_seed, valid, created_at)
           VALUES (?,?,?,?,?,?,?, 1, ?)""",
        (game_id, cup_id, match_no, order_index, slot, home_seed, away_seed, now),
    )

    # 布置起点并开启回合 1（与普通比赛开局一致：左边界自上而下）
    players = conn.execute(
        "SELECT * FROM players WHERE game_id=? ORDER BY rowid", (game_id,)
    ).fetchall()
    blocked = {tuple(c) for c in _loads(cup["blocked_json"])}
    finish = {tuple(c) for c in _loads(cup["finish_json"])}
    starts = [
        (0, y) for y in range(cup["height"])
        if (0, y) not in blocked and (0, y) not in finish
    ]
    if len(starts) < len(players):
        raise ServiceError(409, "not_enough_start_cells", "起点格不足，无法开局")
    board: dict[str, list[int]] = {}
    for player, (x, y) in zip(players, starts):
        conn.execute(
            "UPDATE players SET x=?, y=? WHERE player_id=?", (x, y, player["player_id"])
        )
        board[player["player_id"]] = [x, y]
    deadline = _iso(now_dt + timedelta(seconds=cup["turn_duration_seconds"]))
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
    return game_id


def _find_request(
    conn: sqlite3.Connection, cup_id: str, kind: str, request_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cup_requests WHERE cup_id=? AND kind=? AND request_id=?",
        (cup_id, kind, request_id),
    ).fetchone()


def _assert_request_id_free(
    conn: sqlite3.Connection, cup_id: str, kind: str, request_id: str
) -> None:
    other_kind = "rematch" if kind == "start" else "start"
    clash = conn.execute(
        "SELECT 1 FROM cup_requests WHERE cup_id=? AND kind=? AND request_id=?",
        (cup_id, other_kind, request_id),
    ).fetchone()
    if clash is not None:
        raise ServiceError(
            409, "request_id_conflict", "该请求标识已用于其他操作，请更换标识"
        )


def start_match(
    cup_id: str,
    match_no: int,
    request_id: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        cup = _get_cup(conn, cup_id)
        replay = _find_request(conn, cup_id, "start", request_id)
        if replay is not None:
            if replay["match_no"] != match_no:
                raise ServiceError(
                    409, "request_id_conflict", "该请求标识已用于其他场次"
                )
            result = _loads(replay["result_json"])
            result["idempotent_replay"] = True
            row = conn.execute(
                "SELECT valid FROM cup_games WHERE game_id=?",
                (result["game_id"],),
            ).fetchone()
            result["game_valid"] = bool(row["valid"]) if row is not None else False
            return result
        _assert_request_id_free(conn, cup_id, "start", request_id)
        if match_no not in bracket.BRACKET:
            raise ServiceError(404, "match_not_found", f"场次 {match_no} 不存在")
        if expected_revision is not None and expected_revision != cup["revision"]:
            raise ServiceError(
                409, "stale_revision",
                f"签表已更新（当前版本 {cup['revision']}，请求基于 {expected_revision}），"
                "请重新获取签表后再操作",
            )

        games = _all_cup_games(conn, cup_id)
        player_map = _game_player_map(conn, cup_id)
        results = _valid_results(conn, cup_id, games, player_map)
        if bracket.champion_seed(results) is not None:
            raise ServiceError(409, "cup_finished", "杯赛冠军已产生，不能开赛")
        home, away = bracket.participants(match_no, results)
        if home is None or away is None or home == away:
            raise ServiceError(
                409, "match_not_ready", "该场次两名选手的晋级资格尚未全部确定"
            )
        if any(g["match_no"] == match_no and g["valid"] for g in games):
            raise ServiceError(
                409, "match_already_started", "该场次已有有效对局，不能重复开赛"
            )

        game_id = _create_and_open_game(conn, cup, match_no, home, away)
        now = _iso(_now())
        revision = cup["revision"] + 1
        conn.execute(
            "UPDATE cups SET revision=?, updated_at=? WHERE cup_id=?",
            (revision, now, cup_id),
        )
        result = {
            "cup_id": cup_id,
            "match_no": match_no,
            "game_id": game_id,
            "revision": revision,
            "request_id": request_id,
            "idempotent_replay": False,
            "game_valid": True,
        }
        conn.execute(
            """INSERT INTO cup_requests(cup_id, kind, request_id, match_no,
                                        result_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (cup_id, "start", request_id, match_no, json.dumps(result), now),
        )
        return result

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 重赛：裁定某局失效，连带下游失效
# ---------------------------------------------------------------------------
def rematch(cup_id: str, game_id: str, request_id: str) -> dict[str, Any]:
    def tx(conn: sqlite3.Connection) -> dict[str, Any]:
        cup = _get_cup(conn, cup_id)
        replay = _find_request(conn, cup_id, "rematch", request_id)
        if replay is not None:
            if replay["result_json"]:
                stored = _loads(replay["result_json"])
                if stored["invalidated_game_id"] != game_id:
                    raise ServiceError(
                        409, "request_id_conflict", "该请求标识已用于其他对局"
                    )
                stored["idempotent_replay"] = True
                return stored
        _assert_request_id_free(conn, cup_id, "rematch", request_id)
        target = conn.execute(
            "SELECT * FROM cup_games WHERE cup_id=? AND game_id=?", (cup_id, game_id)
        ).fetchone()
        if target is None:
            raise ServiceError(404, "not_cup_game", "该对局不属于此杯赛")
        if not target["valid"]:
            raise ServiceError(
                409, "game_already_invalidated", "该对局已失效，不能重复重赛"
            )
        game = _get_game(conn, game_id)
        if game["status"] != "finished":
            raise ServiceError(409, "game_not_completed", "只能对已完赛的对局裁定重赛")

        games = _all_cup_games(conn, cup_id)
        player_map = _game_player_map(conn, cup_id)
        results_before = _valid_results(conn, cup_id, games, player_map)
        champion_before = bracket.champion_seed(results_before)

        target_match = target["match_no"]
        downstream_matches = set(bracket.downstream(target_match))
        invalidate_ids = [game_id]
        for g in games:
            if g["game_id"] == game_id:
                continue
            if g["valid"] and g["match_no"] in downstream_matches:
                invalidate_ids.append(g["game_id"])

        turn_no = conn.execute(
            "SELECT MAX(turn_no) AS t FROM turns WHERE game_id=? AND status='closed'",
            (game_id,),
        ).fetchone()["t"]
        now = _iso(_now())
        for gid in invalidate_ids:
            reason = (
                "rematch"
                if gid == game_id
                else f"依赖失效对局 {game_id}（场次 {target_match}）"
            )
            conn.execute(
                "UPDATE cup_games SET valid=0, invalidated_reason=?, invalidated_at=? "
                "WHERE game_id=?",
                (reason, now, gid),
            )

        games_after = _all_cup_games(conn, cup_id)
        results_after = _valid_results(conn, cup_id, games_after, player_map)
        champion_after = bracket.champion_seed(results_after)
        withdrawn = champion_before is not None and champion_after is None

        invalidation_id = new_token(12)
        conn.execute(
            """INSERT INTO cup_invalidations(invalidation_id, cup_id, rematch_request_id,
                                             invalidated_game_id, match_no, scope_slot,
                                             reason, turn_no, propagated_json,
                                             champion_withdrawn, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                invalidation_id, cup_id, request_id, game_id, target_match,
                target["slot"], "referee_rematch", turn_no,
                json.dumps(invalidate_ids[1:]), 1 if withdrawn else 0, now,
            ),
        )
        revision = cup["revision"] + 1
        conn.execute(
            "UPDATE cups SET revision=?, champion_withdrawn=?, updated_at=? "
            "WHERE cup_id=?",
            (revision, 1 if (withdrawn or cup["champion_withdrawn"]) else 0, now, cup_id),
        )
        result = {
            "cup_id": cup_id,
            "invalidated_game_id": game_id,
            "match_no": target_match,
            "revision": revision,
            "request_id": request_id,
            "idempotent_replay": False,
            "invalidated_games": invalidate_ids,
            "champion_withdrawn": withdrawn,
        }
        conn.execute(
            """INSERT INTO cup_requests(cup_id, kind, request_id, match_no,
                                        result_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (cup_id, "rematch", request_id, target_match, json.dumps(result), now),
        )
        return result

    return run_transaction(tx)


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------
def _possible_seeds(side: bracket.Side) -> set[int]:
    memo: dict[tuple[str, int], set[int]] = {}

    def walk(s: bracket.Side) -> set[int]:
        kind, ref = s
        if kind == "seed":
            return {ref}
        key = (kind, ref)
        if key in memo:
            return memo[key]
        d = bracket.BRACKET[ref]
        out = walk(d["home"]) | walk(d["away"])
        memo[key] = out
        return out

    return walk(side)


def _slot_view(
    side: bracket.Side, results: dict[int, tuple[int, int] | None], seed_info: dict
) -> dict[str, Any]:
    seed_no = bracket.side_value(side, results)
    info = seed_info.get(seed_no) if seed_no is not None else None
    return {
        "candidate_seeds": sorted(_possible_seeds(side)),
        "seed_no": seed_no,
        "account_id": info["account_id"] if info else None,
        "name": info["name"] if info else None,
    }


def _build_state(conn: sqlite3.Connection, cup: sqlite3.Row) -> dict[str, Any]:
    cup_id = cup["cup_id"]
    seed_rows = _seed_rows(conn, cup_id)
    seed_info = {
        r["seed_no"]: {
            "seed_no": r["seed_no"], "account_id": r["account_id"], "name": r["name"],
        }
        for r in seed_rows
    }
    games = _all_cup_games(conn, cup_id)
    player_map = _game_player_map(conn, cup_id)
    results = _valid_results(conn, cup_id, games, player_map)

    closed_counts = {
        r["game_id"]: r["c"]
        for r in conn.execute(
            """SELECT t.game_id, COUNT(*) AS c
               FROM turns t
               JOIN cup_games cg ON cg.game_id = t.game_id
               WHERE cg.cup_id=? AND t.status='closed'
               GROUP BY t.game_id""",
            (cup_id,),
        ).fetchall()
    }

    champion = bracket.champion_seed(results)
    games_by_match: dict[int, list[sqlite3.Row]] = {}
    for g in games:
        games_by_match.setdefault(g["match_no"], []).append(g)

    matches: list[dict[str, Any]] = []
    startable: list[int] = []
    for match_no in bracket.MATCH_ORDER:
        meta = bracket.BRACKET[match_no]
        home_seed, away_seed = bracket.participants(match_no, results)
        eligible = (
            home_seed is not None and away_seed is not None and home_seed != away_seed
        )
        mgames = games_by_match.get(match_no, [])
        valid_games = [g for g in mgames if g["valid"]]
        completed = [g for g in valid_games if g["winner_player_id"] is not None]
        current = completed[0] if completed else None
        active = valid_games[0] if valid_games else None
        can_start = eligible and not valid_games and champion is None

        if current is not None:
            status = "finished"
        elif active is not None:
            status = "in_progress"
        elif eligible:
            status = "awaiting_players"
        else:
            status = "pending"

        winner_seed = loser_seed = None
        if current is not None:
            seats = player_map.get(current["game_id"], {})
            w = seats.get(current["winner_player_id"])
            l = seats.get(current["loser_player_id"]) if current["loser_player_id"] else None
            winner_seed = w["seed_no"] if w else None
            loser_seed = l["seed_no"] if l else None

        game_links = []
        for g in mgames:
            seats = player_map.get(g["game_id"], {})
            w = seats.get(g["winner_player_id"]) if g["winner_player_id"] else None
            game_links.append(
                {
                    "game_id": g["game_id"],
                    "cup_id": cup_id,
                    "match_no": g["match_no"],
                    "order_index": g["order_index"],
                    "slot": g["slot"],
                    "status": "completed" if g["winner_player_id"] else "in_progress",
                    "valid": bool(g["valid"]),
                    "home_seed_no": g["home_seed"],
                    "away_seed_no": g["away_seed"],
                    "winner_account_id": w["account_id"] if w else None,
                    "announcement_turns": closed_counts.get(g["game_id"], 0),
                    "invalidated_reason": g["invalidated_reason"],
                    "invalidated_at": g["invalidated_at"],
                }
            )

        if can_start:
            startable.append(match_no)
        matches.append(
            {
                "match_no": match_no,
                "bracket": meta["bracket"],
                "round_no": meta["round_no"],
                "label": meta["label"],
                "eligible": eligible,
                "startable": can_start,
                "active_game_id": active["game_id"] if active else None,
                "current_game_id": current["game_id"] if current else None,
                "status": status,
                "home": _slot_view(meta["home"], results, seed_info),
                "away": _slot_view(meta["away"], results, seed_info),
                "winner_seed_no": winner_seed,
                "loser_seed_no": loser_seed,
                "games": game_links,
            }
        )

    # 名次
    losses = {r["seed_no"]: 0 for r in seed_rows}
    for res in results.values():
        if res is not None and res[1] is not None:
            losses[res[1]] = losses.get(res[1], 0) + 1
    runner_up = None
    if champion is not None:
        if 15 in results and results[15] is not None:
            runner_up = results[15][1]
        elif 14 in results and results[14] is not None:
            runner_up = results[14][1]
    standings = []
    for r in seed_rows:
        seed = r["seed_no"]
        if seed == champion:
            st = "champion"
        elif seed == runner_up:
            st = "runner_up"
        elif losses.get(seed, 0) >= 2:
            st = "eliminated"
        elif losses.get(seed, 0) == 1:
            st = "loser"
        else:
            st = "winner"
        standings.append(
            {
                "seed_no": seed,
                "account_id": r["account_id"],
                "name": r["name"],
                "status": st,
            }
        )

    invalidations = []
    for row in conn.execute(
        "SELECT * FROM cup_invalidations WHERE cup_id=? ORDER BY created_at, rowid",
        (cup_id,),
    ).fetchall():
        invalidations.append(
            {
                "invalidation_id": row["invalidation_id"],
                "rematch_request_id": row["rematch_request_id"],
                "invalidated_game_id": row["invalidated_game_id"],
                "match_no": row["match_no"],
                "scope_slot": row["scope_slot"],
                "reason": row["reason"],
                "turn_no": row["turn_no"],
                "invalidated_at": row["created_at"],
                "propagated_game_ids": _loads(row["propagated_json"]) or [],
                "champion_withdrawn": bool(row["champion_withdrawn"]),
            }
        )

    request_log = [
        {
            "kind": row["kind"],
            "request_id": row["request_id"],
            "match_no": row["match_no"],
            "result": _loads(row["result_json"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM cup_requests WHERE cup_id=? ORDER BY created_at, rowid",
            (cup_id,),
        ).fetchall()
    ]

    if champion is not None:
        status = "finished"
    elif not games:
        status = "setup"
    else:
        status = "active"

    champ_info = seed_info.get(champion) if champion is not None else None
    return {
        "cup_id": cup_id,
        "name": cup["name"],
        "status": status,
        "revision": cup["revision"],
        "width": cup["width"],
        "height": cup["height"],
        "blocked": _loads(cup["blocked_json"]),
        "finish": _loads(cup["finish_json"]),
        "turn_duration_seconds": cup["turn_duration_seconds"],
        "seeds": [
            {
                "seed_no": r["seed_no"],
                "account_id": r["account_id"],
                "name": r["name"],
            }
            for r in seed_rows
        ],
        "matches": matches,
        "standings": standings,
        "startable_matches": startable,
        "champion_seed_no": champion,
        "champion_account_id": champ_info["account_id"] if champ_info else None,
        "champion_name": champ_info["name"] if champ_info else None,
        "champion_withdrawn": bool(cup["champion_withdrawn"]),
        "invalidations": invalidations,
        "request_log": request_log,
    }


def get_cup_state(cup_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        cup = _get_cup(conn, cup_id)
        return _build_state(conn, cup)
    finally:
        conn.close()


def list_cups() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        cup_rows = conn.execute("SELECT * FROM cups ORDER BY created_at").fetchall()
        out = []
        for cup in cup_rows:
            state = _build_state(conn, cup)
            out.append(
                {
                    "cup_id": cup["cup_id"],
                    "name": cup["name"],
                    "status": state["status"],
                    "revision": state["revision"],
                    "champion_account_id": state["champion_account_id"],
                }
            )
        return out
    finally:
        conn.close()


def get_game_cup(game_id: str) -> dict[str, Any]:
    """对局 -> 所属杯赛场次（关联对局查询）。"""
    conn = get_conn()
    try:
        _get_game(conn, game_id)
        row = conn.execute(
            "SELECT * FROM cup_games WHERE game_id=?", (game_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "not_cup_game", "该对局不属于任何杯赛")
        seats = _game_player_map(conn, row["cup_id"]).get(game_id, {})
        w = seats.get(row["winner_player_id"]) if row["winner_player_id"] else None
        ann = conn.execute(
            "SELECT COUNT(*) AS c FROM turns WHERE game_id=? AND status='closed'",
            (game_id,),
        ).fetchone()["c"]
        return {
            "cup_id": row["cup_id"],
            "match_no": row["match_no"],
            "order_index": row["order_index"],
            "slot": row["slot"],
            "valid": bool(row["valid"]),
            "home_seed_no": row["home_seed"],
            "away_seed_no": row["away_seed"],
            "winner_account_id": w["account_id"] if w else None,
            "announcement_turns": ann,
            "invalidated_reason": row["invalidated_reason"],
            "invalidated_at": row["invalidated_at"],
        }
    finally:
        conn.close()


def list_cup_games(cup_id: str) -> list[dict[str, Any]]:
    """杯赛全部关联对局（含已失效），按场次与开赛顺序。"""
    conn = get_conn()
    try:
        cup = _get_cup(conn, cup_id)
        seats_map = _game_player_map(conn, cup_id)
        out = []
        for row in _all_cup_games(conn, cup_id):
            seats = seats_map.get(row["game_id"], {})
            w = seats.get(row["winner_player_id"]) if row["winner_player_id"] else None
            ann = conn.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE game_id=? AND status='closed'",
                (row["game_id"],),
            ).fetchone()["c"]
            out.append(
                {
                    "cup_id": cup_id,
                    "game_id": row["game_id"],
                    "match_no": row["match_no"],
                    "order_index": row["order_index"],
                    "slot": row["slot"],
                    "status": "completed" if row["winner_player_id"] else "in_progress",
                    "valid": bool(row["valid"]),
                    "home_seed_no": row["home_seed"],
                    "away_seed_no": row["away_seed"],
                    "winner_account_id": w["account_id"] if w else None,
                    "announcement_turns": ann,
                    "invalidated_reason": row["invalidated_reason"],
                    "invalidated_at": row["invalidated_at"],
                }
            )
        return out
    finally:
        conn.close()
