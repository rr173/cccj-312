"""HTTP 集成测试：生命周期、信息可见性、接手语义与裁定结果。"""
from __future__ import annotations

from .conftest import (
    close,
    device_headers,
    make_game,
    start,
    submit,
    takeover_headers,
)


def _creds(game: dict, name: str) -> dict:
    return next(p for p in game["players"] if p["name"] == name)


def test_referee_endpoints_require_token(client) -> None:
    resp = client.post(
        "/referee/games",
        json={"width": 5, "height": 5, "finish": [[4, 0]], "players": ["a"]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_create_start_assigns_starting_positions(client) -> None:
    game = make_game(client)
    state = client.get(f"/games/{game['game_id']}").json()
    assert state["status"] == "setup"
    assert state["turn"] is None
    assert state["board"] == {}

    start(client, game["game_id"])
    state = client.get(f"/games/{game['game_id']}").json()
    assert state["status"] == "active"
    assert state["current_turn_no"] == 1
    assert state["turn"]["status"] == "open"
    positions = {p["name"]: tuple(p["position"]) for p in state["players"]}
    # 左边界自上而下
    assert positions == {"alice": (0, 0), "bob": (0, 1), "carol": (0, 2)}


def test_submit_replace_and_reconnect_confirmation(client) -> None:
    game = make_game(client)
    gid = game["game_id"]
    alice = _creds(game, "alice")
    start(client, gid)

    r1 = submit(client, gid, alice["device_token"], "e")
    assert r1.status_code == 201
    assert r1.json()["sequence"] == 1

    # 截止前可替换（alice 在 (0,0)，向南合法）
    r2 = submit(client, gid, alice["device_token"], "s")
    assert r2.status_code == 201
    assert r2.json()["sequence"] == 2

    # 重连确认本回合指令仍可更改，且生效的是最后版本
    me = client.get(f"/games/{gid}/me", headers=device_headers(alice["device_token"])).json()
    assert me["accepted_direction"] == "s"
    assert me["can_replace"] is True
    assert me["replaceable_reason"] == "open"

    # 裁定员视角能看到指令；公开视角看不到任何提交进度
    detail = client.get(
        f"/referee/games/{gid}/turn", headers=client.headers_ref
    ).json()
    alice_cmd = next(c for c in detail["commands"] if c["player_id"] == alice["player_id"])
    assert alice_cmd["direction"] == "s" and alice_cmd["sequence"] == 2
    public = client.get(f"/games/{gid}").json()
    assert "commands" not in public and "direction" not in str(public)


def test_takeover_revokes_old_device_but_keeps_command(client) -> None:
    game = make_game(client)
    gid = game["game_id"]
    alice = _creds(game, "alice")
    start(client, gid)
    submit(client, gid, alice["device_token"], "e")

    # 新设备接手
    resp = client.post(
        f"/games/{gid}/players/{alice['player_id']}/takeover",
        headers=takeover_headers(alice["takeover_secret"]),
    )
    assert resp.status_code == 201
    body = resp.json()
    new_token = body["device_token"]
    assert body["device_generation"] == alice["device_generation"] + 1
    # 已接受的指令保留，且新设备可替换
    assert body["accepted_direction"] == "e"
    assert body["can_replace"] is True

    # 旧设备立即不能再写
    old = submit(client, gid, alice["device_token"], "w")
    assert old.status_code == 409
    assert old.json()["error"] == "device_superseded"

    # 新设备替换成功
    replaced = submit(client, gid, new_token, "wait")
    assert replaced.status_code == 201
    assert replaced.json()["sequence"] == 2


def test_bad_takeover_secret_rejected(client) -> None:
    game = make_game(client)
    alice = _creds(game, "alice")
    resp = client.post(
        f"/games/{game['game_id']}/players/{alice['player_id']}/takeover",
        headers=takeover_headers("wrong-secret"),
    )
    assert resp.status_code == 401


def test_bad_device_token_rejected(client) -> None:
    game = make_game(client)
    start(client, game["game_id"])
    resp = client.get(
        f"/games/{game['game_id']}/me", headers=device_headers("nope")
    )
    assert resp.status_code == 401


def test_invalid_moves_rejected_at_submission(client) -> None:
    game = make_game(client, blocked=[[1, 0]])
    gid = game["game_id"]
    alice = _creds(game, "alice")
    start(client, gid)
    # alice 在 (0,0)，东侧阻挡、西侧越界
    assert submit(client, gid, alice["device_token"], "e").status_code == 422
    assert submit(client, gid, alice["device_token"], "w").status_code == 422
    assert submit(client, gid, alice["device_token"], "wait").status_code == 201


def test_close_turn_adjudicates_and_opens_next_turn_atomically(client) -> None:
    game = make_game(client)
    gid = game["game_id"]
    alice, bob = _creds(game, "alice"), _creds(game, "bob")
    start(client, gid)

    submit(client, gid, alice["device_token"], "e")
    submit(client, gid, bob["device_token"], "e")
    # carol 未交指令 -> 等待

    ann = close(client, gid)
    assert ann["turn_no"] == 1
    assert ann["game_status"] == "active"
    # 新回合已经公开可玩
    state = client.get(f"/games/{gid}").json()
    assert state["current_turn_no"] == 2 and state["turn"]["status"] == "open"
    # 公告棋盘 == 公开棋盘
    assert state["board"] == ann["board"]

    results = {r["player_id"]: r for r in ann["results"]}
    assert results[alice["player_id"]]["reason"] == "moved"
    assert results[bob["player_id"]]["reason"] == "moved"
    carol = _creds(game, "carol")
    assert results[carol["player_id"]]["reason"] == "no_command"
    assert (results[carol["player_id"]]["x"], results[carol["player_id"]]["y"]) == (0, 2)

    # 同一公告可再次拉取
    again = client.get(f"/games/{gid}/announcements/1")
    assert again.status_code == 200 and again.json() == ann

    # 新回合提交的是 turn 2 的指令
    r = submit(client, gid, alice["device_token"], "e")
    assert r.json()["turn_no"] == 2


def test_concurrent_close_with_expected_turn_no_is_safe(client) -> None:
    game = make_game(client)
    gid = game["game_id"]
    start(client, gid)
    # 两个裁定员同时关回合 1：一个成功，另一个被幂等保护拒绝
    r1 = client.post(
        f"/referee/games/{gid}/close",
        headers=client.headers_ref,
        json={"expected_turn_no": 1},
    )
    r2 = client.post(
        f"/referee/games/{gid}/close",
        headers=client.headers_ref,
        json={"expected_turn_no": 1},
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    # 回合 2 仍然完好可玩，没有被连带裁定
    state = client.get(f"/games/{gid}").json()
    assert state["current_turn_no"] == 2 and state["turn"]["status"] == "open"


def test_swap_follow_and_block_end_to_end(client) -> None:
    # 高度 2：alice(0,0), bob(0,1)，互换
    game = make_game(client, players=["alice", "bob"], height=2, finish=[[4, 0]])
    gid = game["game_id"]
    alice, bob = _creds(game, "alice"), _creds(game, "bob")
    start(client, gid)
    submit(client, gid, alice["device_token"], "s")
    submit(client, gid, bob["device_token"], "n")
    ann = close(client, gid)
    by_name = {
        next(p["name"] for p in game["players"] if p["player_id"] == r["player_id"]): r
        for r in ann["results"]
    }
    assert (by_name["alice"]["x"], by_name["alice"]["y"]) == (0, 1)
    assert (by_name["bob"]["x"], by_name["bob"]["y"]) == (0, 0)

    # 第二回合：alice(0,1) 向东到 (1,1)；bob(0,0) 也向东到 (1,0)，
    # 这是跟进成功。再下一回合同目标：两人都向东 -> 同时进 (2,1)/(2,0)?
    # 直接构造同目标：alice e->(1,1)，bob s 跟进已验证；改为两人都 e 不冲突。
    # 同目标构造：alice 在 (0,1) 走 e 到 (1,1)，bob 在 (0,0) 走 s 到 (0,1)——
    # 这是跟进；真正同目标需要两人位置相邻且对角：
    # 先让 alice 等待、bob 南移到 (0,1) 会被等待者堵住。
    # 采用：alice e 到 (1,1)，bob e 到 (1,0) 后，下一回合两人都 e 分别到 (2,*)。
    # 最直接：本回合两人都提交 e 后位置是 (1,1) 与 (1,0)，
    # 再下一回合 alice 等，bob s -> 进 alice 格，alice 不动 -> bob 被堵。
    submit(client, gid, alice["device_token"], "e")
    submit(client, gid, bob["device_token"], "e")
    close(client, gid)
    # 现在 alice(1,1)、bob(1,0)；alice 等待，bob 南移想进入 alice 的格 -> 被堵
    submit(client, gid, alice["device_token"], "wait")
    submit(client, gid, bob["device_token"], "s")
    ann3 = close(client, gid)
    blocked = {r["player_id"]: r for r in ann3["results"]}
    assert blocked[alice["player_id"]]["reason"] == "waiting"
    assert blocked[bob["player_id"]]["reason"] == "blocked_by_staying_occupant"
    board = client.get(f"/games/{gid}").json()["board"]
    assert board[alice["player_id"]] == [1, 1]
    assert board[bob["player_id"]] == [1, 0]


def test_shared_target_collision_end_to_end(client) -> None:
    # 三玩家纵向排列于 x=0；alice(0,0) 与 carol(0,2) 都要进 (0,1)
    game = make_game(client)  # alice(0,0), bob(0,1), carol(0,2)
    gid = game["game_id"]
    alice, bob, carol = (_creds(game, n) for n in ("alice", "bob", "carol"))
    start(client, gid)
    submit(client, gid, alice["device_token"], "s")  # ->(0,1)
    submit(client, gid, carol["device_token"], "n")  # ->(0,1)
    submit(client, gid, bob["device_token"], "e")    # 让出 (0,1) 也不影响规则2
    ann = close(client, gid)
    by_id = {r["player_id"]: r for r in ann["results"]}
    assert by_id[alice["player_id"]]["reason"] == "collision_target_shared"
    assert by_id[carol["player_id"]]["reason"] == "collision_target_shared"
    assert (by_id[alice["player_id"]]["x"], by_id[alice["player_id"]]["y"]) == (0, 0)
    assert (by_id[carol["player_id"]]["x"], by_id[carol["player_id"]]["y"]) == (0, 2)
    # bob 正常移动
    assert by_id[bob["player_id"]]["reason"] == "moved"


def test_finish_line_ends_game_and_blocks_new_commands(client) -> None:
    # 单人，直线冲向终点 (4,0)
    game = make_game(client, players=["solo"], width=5, height=1, finish=[[4, 0]])
    gid = game["game_id"]
    solo = _creds(game, "solo")
    start(client, gid)
    for _ in range(4):
        r = submit(client, gid, solo["device_token"], "e")
        assert r.status_code == 201
        close(client, gid)
    state = client.get(f"/games/{gid}").json()
    assert state["status"] == "finished"
    assert state["winners"] == [solo["player_id"]]
    # 游戏结束后无法再提交
    resp = submit(client, gid, solo["device_token"], "e")
    assert resp.status_code == 409
    # 游戏结束后没有可关闭的回合
    close_resp = client.post(
        f"/referee/games/{gid}/close", headers=client.headers_ref
    )
    assert close_resp.status_code == 409
    assert close_resp.json()["error"] == "game_finished"
    me = client.get(f"/games/{gid}/me", headers=device_headers(solo["device_token"])).json()
    assert me["can_replace"] is False


def test_cannot_close_before_start(client) -> None:
    game = make_game(client)
    resp = client.post(
        f"/referee/games/{game['game_id']}/close", headers=client.headers_ref
    )
    assert resp.status_code == 409
