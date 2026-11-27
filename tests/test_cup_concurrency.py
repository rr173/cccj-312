"""杯赛并发测试（真实 uvicorn 服务器 + 裸线程并发请求）。

验证：
1. 并发开赛场次：恰好一局有效；
2. 最后一回合裁定与重赛并发：要么裁定后重赛生效，要么重赛前后者被拒，
   最终始终满足“该场次至多一局有效”，且裁定后不能凭已失效对局继续；
3. 并发重赛同一局：只有一次生效，其余被拒或幂等重放。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest
import uvicorn

from .test_concurrency import ServerHandle, run_concurrently

REFEREE = {"X-Referee-Token": "cup-conc-token"}
NAMES = [f"p{i}" for i in range(1, 9)]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> ServerHandle:
    tmpdir = tempfile.mkdtemp(prefix="race-cup-conc-")
    monkeypatch.setenv("RACE_DB_PATH", os.path.join(tmpdir, "conc.db"))
    monkeypatch.setenv("RACE_REFEREE_TOKEN", "cup-conc-token")

    from race_server.main import app  # 延迟导入以读取环境变量

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server_obj = uvicorn.Server(config)
    thread = threading.Thread(target=server_obj.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server_obj.started:
            break
        time.sleep(0.05)
    assert server_obj.started
    port = server_obj.servers[0].sockets[0].getsockname()[1]
    handle = ServerHandle(f"http://127.0.0.1:{port}")
    for _ in range(100):
        try:
            if handle.get("/health").status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.05)
    yield handle
    server_obj.should_exit = True
    thread.join(timeout=10)


def _setup_cup(s: ServerHandle, width: int = 3, height: int = 2,
               finish: list[list[int]] | None = None) -> tuple[dict, dict]:
    accounts = {}
    for n in NAMES:
        r = s.post("/referee/accounts", headers=REFEREE,
                   json={"name": n, "initial_points": 0})
        assert r.status_code == 201, r.text
        accounts[n] = r.json()
    r = s.post(
        "/referee/cups", headers=REFEREE,
        json={
            "name": "conc", "width": width, "height": height,
            "finish": finish or [[2, 0], [2, 1]],
            "turn_duration_seconds": 60, "seeds": NAMES,
        },
    )
    assert r.status_code == 201, r.text
    return r.json(), accounts


def _state(s: ServerHandle, cup_id: str) -> dict:
    r = s.get(f"/cups/{cup_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _start(s: ServerHandle, cup_id: str, match_no: int, req_id: str):
    return s.post(
        f"/referee/cups/{cup_id}/matches/{match_no}/start",
        headers=REFEREE, json={"request_id": req_id},
    )


def _play(s: ServerHandle, cup: dict, accounts: dict, cup_id: str, match_no: int,
          winner_seed: int) -> str:
    state = _state(s, cup_id)
    mv = next(m for m in state["matches"] if m["match_no"] == match_no)
    game_id = mv["active_game_id"]
    assert game_id
    seeds = {x["seed_no"]: x for x in state["seeds"]}
    home, away = mv["home"]["seed_no"], mv["away"]["seed_no"]
    loser = away if winner_seed == home else home
    players = s.get(f"/games/{game_id}").json()["players"]
    names = {p["name"]: p["player_id"] for p in players}
    tokens = {}
    for seed_no in (home, away):
        name = seeds[seed_no]["name"]
        t = s.post(
            f"/games/{game_id}/players/{names[name]}/takeover",
            headers={"X-Takeover-Secret": accounts[name]["account_secret"]},
        )
        assert t.status_code == 201, t.text
        tokens[seed_no] = t.json()["device_token"]

    def turn(dirs):
        for seed_no, d in dirs.items():
            if d is not None:
                r = s.post(f"/games/{game_id}/commands",
                           headers={"X-Device-Token": tokens[seed_no]},
                           json={"direction": d})
                assert r.status_code == 201, r.text
        c = s.post(f"/referee/games/{game_id}/close", headers=REFEREE)
        assert c.status_code == 200, c.text
        return c.json()

    turn({winner_seed: "e", loser: "wait"})
    turn({winner_seed: "e", loser: "wait"})
    turn({loser: "e"})
    turn({loser: "e"})
    return game_id


def test_concurrent_start_opens_exactly_one_game(server: ServerHandle) -> None:
    cup, _ = _setup_cup(server)
    gid = cup["cup_id"]
    barrier = threading.Barrier(6)

    def job(i: int):
        def run():
            barrier.wait()
            return _start(server, gid, 1, f"start-{i}")
        return run

    resps = run_concurrently([job(i) for i in range(6)])
    ok = [r for r in resps if r.status_code == 200 and not r.json()["idempotent_replay"]]
    assert len(ok) == 1
    game_ids = {r.json()["game_id"] for r in resps if r.status_code == 200}
    assert len(game_ids) == 1  # 幂等重放也指向同一局
    for r in resps:
        if r.status_code != 200:
            assert r.json()["error"] == "match_already_started"

    state = _state(server, gid)
    m1 = next(m for m in state["matches"] if m["match_no"] == 1)
    assert [g["game_id"] for g in m1["games"] if g["valid"]] == [ok[0].json()["game_id"]]


def test_concurrent_rematch_same_game_applies_once(server: ServerHandle) -> None:
    cup, accounts = _setup_cup(server)
    gid = cup["cup_id"]
    _start(server, gid, 1, "start-1")
    game1 = _play(server, cup, accounts, gid, 1, winner_seed=1)

    barrier = threading.Barrier(5)

    def job(i: int):
        def run():
            barrier.wait()
            return server.post(
                f"/referee/cups/{gid}/games/{game1}/rematch",
                headers=REFEREE, json={"request_id": f"rm-{i}"},
            )
        return run

    resps = run_concurrently([job(i) for i in range(5)])
    applied = [r for r in resps if r.status_code == 200]
    assert len(applied) == 1
    for r in resps:
        if r.status_code != 200:
            assert r.json()["error"] == "game_already_invalidated"

    state = _state(server, gid)
    m1 = next(m for m in state["matches"] if m["match_no"] == 1)
    assert all(not g["valid"] for g in m1["games"])
    assert len(state["invalidations"]) == 1


def test_final_close_racing_rematch_never_leaves_two_valid_games(server: ServerHandle) -> None:
    """最后一个回合的关回合（产生场次胜负）与该场次重赛并发。

    线性化只允许两种结果，且都不能出现“同一场次两局同时有效”：
    - 关回合先获锁 -> 对局完赛，重赛时已完赛因而生效（失效）；
    - 重赛先获锁 -> 对局尚未完赛，重赛被拒（game_not_completed），关回合正常生效。
    """
    cup, accounts = _setup_cup(server)
    gid = cup["cup_id"]
    _start(server, gid, 1, "start-1")
    state = _state(server, gid)
    mv = next(m for m in state["matches"] if m["match_no"] == 1)
    game1 = mv["active_game_id"]
    seeds = {x["seed_no"]: x for x in state["seeds"]}
    home, away = mv["home"]["seed_no"], mv["away"]["seed_no"]
    players = server.get(f"/games/{game1}").json()["players"]
    names = {p["name"]: p["player_id"] for p in players}
    tokens = {}
    for seed_no in (home, away):
        name = seeds[seed_no]["name"]
        t = server.post(
            f"/games/{game1}/players/{names[name]}/takeover",
            headers={"X-Takeover-Secret": accounts[name]["account_secret"]},
        )
        assert t.status_code == 201, t.text
        tokens[seed_no] = t.json()["device_token"]

    # 回合 1-3 正常打完（胜者两回合到终点，负者逐步靠近）
    for dirs in ({home: "e", away: "wait"}, {home: "e", away: "wait"}, {away: "e"}):
        for seed_no, d in dirs.items():
            r = server.post(
                f"/games/{game1}/commands",
                headers={"X-Device-Token": tokens[seed_no]}, json={"direction": d},
            )
            assert r.status_code == 201, r.text
        assert server.post(f"/referee/games/{game1}/close", headers=REFEREE).status_code == 200

    # 最后一回合：负者到达终点即全员完赛，场次胜负产生
    r = server.post(
        f"/games/{game1}/commands",
        headers={"X-Device-Token": tokens[away]}, json={"direction": "e"},
    )
    assert r.status_code == 201, r.text

    barrier = threading.Barrier(2)

    def final_close():
        barrier.wait()
        return server.post(f"/referee/games/{game1}/close", headers=REFEREE)

    def rematch_call():
        barrier.wait()
        return server.post(
            f"/referee/cups/{gid}/games/{game1}/rematch",
            headers=REFEREE, json={"request_id": "rm-final"},
        )

    close_resp, rm = run_concurrently([final_close, rematch_call])

    state = _state(server, gid)
    m1 = next(m for m in state["matches"] if m["match_no"] == 1)
    valid = [g for g in m1["games"] if g["valid"]]
    # 不变量：同一场次至多一局有效
    assert len(valid) <= 1
    # 关回合要么在本局上成功，要么因该局已被重赛失效而被拒
    assert close_resp.status_code in (200, 409)
    if close_resp.status_code == 409:
        assert close_resp.json()["error"] == "cup_game_invalidated"
        # 重赛先获锁并生效：该局失效，场次回到可开赛
        assert rm.status_code == 200
        assert valid == []
        assert m1["startable"] is True
        final = server.get(f"/games/{game1}").json()
        assert final["status"] == "active"  # 未完赛，胜负未产生
    else:
        # 关回合先获锁：该局完赛，场次胜负已产生。
        # 随后的重赛若生效，会把该局连同场次结果一并作废（回到可开赛）；
        # 若重赛在完赛前获锁，则被拒（game_not_completed），场次保持已完赛。
        assert server.get(f"/games/{game1}").json()["status"] == "finished"
        if rm.status_code == 200:
            assert valid == []
            assert m1["startable"] is True
            assert m1["status"] == "awaiting_players"
        else:
            assert rm.status_code == 409
            assert rm.json()["error"] == "game_not_completed"
            assert [g["game_id"] for g in valid] == [game1]
            assert m1["status"] == "finished"
            assert m1["winner_seed_no"] == home

    # 无论如何都只能开一局新的，且旧局不会再变为有效
    if m1["startable"]:
        r = _start(server, gid, 1, "start-1b")
        assert r.status_code == 200
        m1 = next(
            m for m in _state(server, gid)["matches"] if m["match_no"] == 1
        )
        assert [g["game_id"] for g in m1["games"] if g["valid"]] == [r.json()["game_id"]]
