"""积分赛功能测试：账号、报名/撤回幂等、发车条件、接手、结算、取消与查询。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import close, device_headers, start, takeover_headers


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def make_account(client: TestClient, name: str, points: int = 0) -> dict:
    resp = client.post(
        "/referee/accounts",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={"name": name, "initial_points": points},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_stakes_game(
    client: TestClient,
    roster: list[str],
    fee: int,
    *,
    width: int = 5,
    height: int = 5,
    blocked: list[list[int]] | None = None,
    finish: list[list[int]] | None = None,
) -> dict:
    resp = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={
            "name": "stakes",
            "width": width,
            "height": height,
            "blocked": blocked or [],
            "finish": finish or [[4, 0]],
            "turn_duration_seconds": 60,
            "roster": roster,
            "entry_fee": fee,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def account_headers(secret: str) -> dict[str, str]:
    return {"X-Account-Secret": secret}


def enroll(client: TestClient, gid: str, secret: str, req_id: str):
    return client.post(
        f"/games/{gid}/stakes/enroll",
        headers=account_headers(secret),
        json={"request_id": req_id},
    )


def withdraw(client: TestClient, gid: str, secret: str, req_id: str):
    return client.post(
        f"/games/{gid}/stakes/withdraw",
        headers=account_headers(secret),
        json={"request_id": req_id},
    )


def me(client: TestClient, secret: str) -> dict:
    resp = client.get("/accounts/me", headers=account_headers(secret))
    assert resp.status_code == 200, resp.text
    return resp.json()


def stakes_state(client: TestClient, gid: str) -> dict:
    resp = client.get(f"/games/{gid}/stakes")
    assert resp.status_code == 200, resp.text
    return resp.json()


def takeover_seat(client: TestClient, gid: str, player_id: str, secret: str) -> dict:
    resp = client.post(
        f"/games/{gid}/players/{player_id}/takeover",
        headers=takeover_headers(secret),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def roster_of(game: dict, name: str) -> dict:
    return next(r for r in game["roster"] if r["name"] == name)


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
def test_create_account_and_validation(client) -> None:
    acc = make_account(client, "alice", 100)
    assert acc["available_points"] == 100
    assert acc["account_secret"]

    # 非负整数：负数被拒
    bad = client.post(
        "/referee/accounts",
        headers=client.headers_ref,
        json={"name": "bad", "initial_points": -1},
    )
    assert bad.status_code == 422

    # 重名被拒
    dup = client.post(
        "/referee/accounts",
        headers=client.headers_ref,
        json={"name": "alice", "initial_points": 0},
    )
    assert dup.status_code == 409
    assert dup.json()["error"] == "account_name_taken"

    # 需要裁定员令牌
    noauth = client.post("/referee/accounts", json={"name": "x", "initial_points": 0})
    assert noauth.status_code == 401

    # 裁定员列表可见；选手自查可见初始积分流水
    listed = client.get("/referee/accounts", headers=client.headers_ref).json()
    assert [a["name"] for a in listed] == ["alice"]
    mine = me(client, acc["account_secret"])
    assert mine["available_points"] == 100
    assert mine["holds"] == []
    assert mine["ledger"][0]["kind"] == "initial"
    assert mine["ledger"][0]["amount"] == 100

    # 错误密钥
    bad_me = client.get("/accounts/me", headers=account_headers("nope"))
    assert bad_me.status_code == 401


def test_stakes_game_creation_validation(client) -> None:
    make_account(client, "alice", 10)
    # 名单中的账号必须存在
    missing = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,
        json={"finish": [[4, 0]], "roster": ["alice", "ghost"], "entry_fee": 5},
    )
    assert missing.status_code == 422
    assert missing.json()["error"] == "account_not_found"
    # 名单不可重复
    dup = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,
        json={"finish": [[4, 0]], "roster": ["alice", "alice"], "entry_fee": 5},
    )
    assert dup.status_code == 422
    # 报名费必须为正整数
    zero_fee = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,
        json={"finish": [[4, 0]], "roster": ["alice"], "entry_fee": 0},
    )
    assert zero_fee.status_code == 422
    # 地图校验沿用普通比赛规则
    bad_map = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,
        json={"finish": [], "roster": ["alice"], "entry_fee": 5},
    )
    assert bad_map.status_code == 422


# ---------------------------------------------------------------------------
# 报名与撤回
# ---------------------------------------------------------------------------
def test_enroll_holds_points_and_replay_is_idempotent(client) -> None:
    acc = make_account(client, "alice", 10)
    make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]

    r = enroll(client, gid, acc["account_secret"], "req-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "enrolled"
    assert body["available_points"] == 5
    assert body["idempotent_replay"] is False

    # 同一请求标识重试：返回首次结果，不重复占用
    again = enroll(client, gid, acc["account_secret"], "req-1")
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert me(client, acc["account_secret"])["available_points"] == 5
    holds = me(client, acc["account_secret"])["holds"]
    assert len(holds) == 1 and holds[0]["entry_fee"] == 5
    hold_entries = [
        e for e in me(client, acc["account_secret"])["ledger"] if e["kind"] == "hold"
    ]
    assert len(hold_entries) == 1 and hold_entries[0]["amount"] == -5

    # 换请求标识重复报名：拒绝，不重复占用
    dup = enroll(client, gid, acc["account_secret"], "req-2")
    assert dup.status_code == 409
    assert dup.json()["error"] == "already_enrolled"
    assert me(client, acc["account_secret"])["available_points"] == 5

    # 请求标识不能挪作撤回用
    conflict = withdraw(client, gid, acc["account_secret"], "req-1")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "request_id_conflict"

    # 名单外账号不能报名
    outsider = make_account(client, "outsider", 100)
    resp = enroll(client, gid, outsider["account_secret"], "req-x")
    assert resp.status_code == 403
    assert resp.json()["error"] == "not_in_roster"


def test_enroll_insufficient_points_changes_nothing(client) -> None:
    acc = make_account(client, "alice", 3)
    make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]

    r = enroll(client, gid, acc["account_secret"], "req-1")
    assert r.status_code == 409
    assert r.json()["error"] == "insufficient_points"
    # 报名与积分均未改变
    mine = me(client, acc["account_secret"])
    assert mine["available_points"] == 3
    assert mine["holds"] == []
    assert all(e["kind"] != "hold" for e in mine["ledger"])
    assert stakes_state(client, gid)["enrolled"] == 0


def test_withdraw_releases_points_and_replay_is_idempotent(client) -> None:
    acc = make_account(client, "alice", 10)
    make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]
    enroll(client, gid, acc["account_secret"], "enroll-1")

    w = withdraw(client, gid, acc["account_secret"], "wd-1")
    assert w.status_code == 200, w.text
    assert w.json()["status"] == "withdrawn"
    assert w.json()["available_points"] == 10

    # 重试同一撤回：不重复返还
    again = withdraw(client, gid, acc["account_secret"], "wd-1")
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    mine = me(client, acc["account_secret"])
    assert mine["available_points"] == 10
    releases = [e for e in mine["ledger"] if e["kind"] == "release"]
    assert len(releases) == 1 and releases[0]["amount"] == 5

    # 没有可撤回的报名（新请求标识）
    none_left = withdraw(client, gid, acc["account_secret"], "wd-2")
    assert none_left.status_code == 409
    assert none_left.json()["error"] == "not_enrolled"


def test_old_withdraw_replay_cannot_cancel_new_enrollment(client) -> None:
    acc = make_account(client, "alice", 10)
    make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]
    secret = acc["account_secret"]

    enroll(client, gid, secret, "enroll-1")
    withdraw(client, gid, secret, "wd-1")
    # 重新报名（新请求标识）
    r2 = enroll(client, gid, secret, "enroll-2")
    assert r2.status_code == 200
    assert r2.json()["epoch"] == 2
    assert me(client, secret)["available_points"] == 5

    # 旧撤回请求重试：返回历史结果，但后来的报名仍然有效
    replay = withdraw(client, gid, secret, "wd-1")
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["epoch"] == 1
    mine = me(client, secret)
    assert mine["available_points"] == 5  # 没有被再次返还
    assert len(mine["holds"]) == 1
    roster = stakes_state(client, gid)["roster"]
    assert roster[0]["enrollment"] == "enrolled"


def test_enroll_withdraw_closed_after_start(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-a")
    enroll(client, gid, b["account_secret"], "e-b")
    start(client, gid)

    r = enroll(client, gid, a["account_secret"], "e-a2")
    assert r.status_code == 409 and r.json()["error"] == "enrollment_closed"
    w = withdraw(client, gid, a["account_secret"], "w-a")
    assert w.status_code == 409 and w.json()["error"] == "enrollment_closed"


def test_start_requires_full_roster_enrollment(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]

    resp = client.post(f"/referee/games/{gid}/start", headers=client.headers_ref)
    assert resp.status_code == 409
    assert resp.json()["error"] == "roster_not_fully_enrolled"

    enroll(client, gid, a["account_secret"], "e-a")
    resp = client.post(f"/referee/games/{gid}/start", headers=client.headers_ref)
    assert resp.status_code == 409

    enroll(client, gid, b["account_secret"], "e-b")
    started = start(client, gid)
    assert started["status"] == "active"
    state = stakes_state(client, gid)
    assert state["status"] == "active"
    assert state["pot"] == 10
    assert state["enrolled"] == 2


# ---------------------------------------------------------------------------
# 接手与对局
# ---------------------------------------------------------------------------
def test_takeover_requires_enrollment_and_account_secret(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]
    seat_a = roster_of(game, "alice")

    # 未报名不能接手
    resp = client.post(
        f"/games/{gid}/players/{seat_a['player_id']}/takeover",
        headers=takeover_headers(a["account_secret"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_enrolled"

    enroll(client, gid, a["account_secret"], "e-a")
    # 密钥错误
    bad = client.post(
        f"/games/{gid}/players/{seat_a['player_id']}/takeover",
        headers=takeover_headers("wrong"),
    )
    assert bad.status_code == 401
    # 已报名可接手，拿到设备令牌
    taken = takeover_seat(client, gid, seat_a["player_id"], a["account_secret"])
    assert taken["device_generation"] == 1
    assert taken["device_token"]

    # bob 的账号密钥不能接手 alice 的席位
    resp = client.post(
        f"/games/{gid}/players/{seat_a['player_id']}/takeover",
        headers=takeover_headers(b["account_secret"]),
    )
    assert resp.status_code == 401


def test_stakes_game_playable_end_to_end(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5, height=2, finish=[[4, 0]])
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-a")
    enroll(client, gid, b["account_secret"], "e-b")
    start(client, gid)

    seat_a = roster_of(game, "alice")
    tok_a = takeover_seat(client, gid, seat_a["player_id"], a["account_secret"])["device_token"]
    # 接手后沿用现有玩法：提交指令、查询自身状态
    r = client.post(
        f"/games/{gid}/commands", headers=device_headers(tok_a), json={"direction": "e"}
    )
    assert r.status_code == 201
    me_state = client.get(f"/games/{gid}/me", headers=device_headers(tok_a)).json()
    assert me_state["accepted_direction"] == "e"
    ann = close(client, gid)
    assert ann["turn_no"] == 1
    assert ann["game_status"] == "active"


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
def _play_straight(client, gid, tokens_dirs):
    """每个元素是 (device_token, [directions...])，逐回合提交并关回合。"""
    max_turns = max(len(d) for _, d in tokens_dirs)
    anns = []
    for i in range(max_turns):
        for token, dirs in tokens_dirs:
            if i < len(dirs):
                r = client.post(
                    f"/games/{gid}/commands",
                    headers=device_headers(token),
                    json={"direction": dirs[i]},
                )
                assert r.status_code == 201, r.text
        anns.append(close(client, gid))
    return anns


def test_payout_only_after_all_finish_and_split(client) -> None:
    # 3 人，报名费 3，奖池 9；alice/bob 第 2 回合同时到达，carol 第 3 回合
    accs = {n: make_account(client, n, 10) for n in ("alice", "bob", "carol")}
    game = make_stakes_game(
        client,
        ["alice", "bob", "carol"],
        3,
        width=3,
        height=3,
        finish=[[2, 0], [2, 1], [2, 2]],
    )
    gid = game["game_id"]
    for n, acc in accs.items():
        enroll(client, gid, acc["account_secret"], f"e-{n}")
    start(client, gid)
    tokens = {
        n: takeover_seat(
            client, gid, roster_of(game, n)["player_id"], accs[n]["account_secret"]
        )["device_token"]
        for n in accs
    }

    # 回合 1：alice/bob 东进，carol 等待
    anns = _play_straight(
        client, gid, [(tokens["alice"], ["e"]), (tokens["bob"], ["e"])]
    )
    assert anns[-1]["game_status"] == "active"
    # 回合 2：alice/bob 到达终点；carol 东进
    anns = _play_straight(
        client,
        gid,
        [(tokens["alice"], ["e"]), (tokens["bob"], ["e"]), (tokens["carol"], ["e"])],
    )
    assert anns[-1]["game_status"] == "active"
    # 此时已有选手到达终点，但尚未全员完赛：不发奖
    for n, acc in accs.items():
        mine = me(client, acc["account_secret"])
        assert mine["available_points"] == 7, n
        assert all(e["kind"] != "payout" for e in mine["ledger"])
    assert stakes_state(client, gid)["settlement"] is None

    # 回合 3：carol 到达终点 -> 全员完赛，结算
    anns = _play_straight(client, gid, [(tokens["carol"], ["e"])])
    final = anns[-1]
    assert final["game_status"] == "finished"
    settlement = final["stakes"]
    assert settlement["pot"] == 9
    assert settlement["entry_fee"] == 3

    # 最早到达的是 alice/bob（第 2 回合）；9 / 2 = 4 余 1，
    # 余点按账号 ID 升序给先者
    winner_ids = sorted(
        [accs["alice"]["account_id"], accs["bob"]["account_id"]]
    )
    assert settlement["winners"] == winner_ids
    payouts = {p["account_id"]: p["amount"] for p in settlement["payouts"]}
    assert payouts[winner_ids[0]] == 5
    assert payouts[winner_ids[1]] == 4
    assert accs["carol"]["account_id"] not in payouts

    # 余额：初始 10 - 报名 3 + 奖金
    assert me(client, accs["alice"]["account_secret"])["available_points"] == 7 + payouts[accs["alice"]["account_id"]]
    assert me(client, accs["bob"]["account_secret"])["available_points"] == 7 + payouts[accs["bob"]["account_id"]]
    assert me(client, accs["carol"]["account_secret"])["available_points"] == 7

    # 分账结果可查询（公开接口 + 最终回合公告）
    state = stakes_state(client, gid)
    assert state["status"] == "finished"
    assert state["settlement"] == settlement
    ann3 = client.get(f"/games/{gid}/announcements/3").json()
    assert ann3["stakes"] == settlement
    # 公开比赛状态为 finished
    assert client.get(f"/games/{gid}").json()["status"] == "finished"

    # 每个赢家只有一条 payout 流水（无重复发奖）
    for n in ("alice", "bob"):
        pays = [
            e for e in me(client, accs[n]["account_secret"])["ledger"]
            if e["kind"] == "payout"
        ]
        assert len(pays) == 1


def test_single_winner_takes_whole_pot(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(
        client, ["alice", "bob"], 5, width=3, height=2, finish=[[2, 0], [2, 1]]
    )
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-a")
    enroll(client, gid, b["account_secret"], "e-b")
    start(client, gid)
    tok_a = takeover_seat(client, gid, roster_of(game, "alice")["player_id"], a["account_secret"])["device_token"]
    tok_b = takeover_seat(client, gid, roster_of(game, "bob")["player_id"], b["account_secret"])["device_token"]

    # alice 两回合到达 (2,0)；bob 等待一回合后走向 (1,1)，再东移进 (2,1)
    _play_straight(client, gid, [(tok_a, ["e"]), (tok_b, ["wait"])])
    _play_straight(client, gid, [(tok_a, ["e"]), (tok_b, ["e"])])
    final = _play_straight(client, gid, [(tok_b, ["e"])])[-1]
    assert final["game_status"] == "finished"
    settlement = final["stakes"]
    assert settlement["pot"] == 10
    assert settlement["winners"] == [a["account_id"]]
    assert settlement["payouts"][0]["amount"] == 10
    assert me(client, a["account_secret"])["available_points"] == 15
    assert me(client, b["account_secret"])["available_points"] == 5


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
def test_cancel_refunds_and_locks_game(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5, height=2, finish=[[4, 0]])
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-a")
    enroll(client, gid, b["account_secret"], "e-b")
    start(client, gid)
    tok_a = takeover_seat(client, gid, roster_of(game, "alice")["player_id"], a["account_secret"])["device_token"]
    client.post(f"/games/{gid}/commands", headers=device_headers(tok_a), json={"direction": "e"})
    close(client, gid)  # 第 1 回合公告

    resp = client.post(f"/referee/games/{gid}/cancel", headers=client.headers_ref)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["refunded_total"] == 10
    assert len(body["refunded"]) == 2

    # 占用全部退回
    for acc in (a, b):
        mine = me(client, acc["account_secret"])
        assert mine["available_points"] == 10
        assert mine["holds"] == []
        refunds = [e for e in mine["ledger"] if e["kind"] == "refund"]
        assert len(refunds) == 1 and refunds[0]["amount"] == 5

    # 历史回合公告保留
    ann1 = client.get(f"/games/{gid}/announcements/1")
    assert ann1.status_code == 200
    # 比赛状态与资金处理一致
    assert client.get(f"/games/{gid}").json()["status"] == "cancelled"
    state = stakes_state(client, gid)
    assert state["status"] == "cancelled"
    assert state["settlement"] is None
    assert state["cancelled_at"]
    assert all(r["enrollment"] == "refunded" for r in state["roster"])

    # 不再接手、提交或裁定新结果
    t = client.post(
        f"/games/{gid}/players/{roster_of(game, 'alice')['player_id']}/takeover",
        headers=takeover_headers(a["account_secret"]),
    )
    assert t.status_code == 409 and t.json()["error"] == "stakes_cancelled"
    s = client.post(
        f"/games/{gid}/commands", headers=device_headers(tok_a), json={"direction": "e"}
    )
    assert s.status_code == 409 and s.json()["error"] == "stakes_cancelled"
    c = client.post(f"/referee/games/{gid}/close", headers=client.headers_ref)
    assert c.status_code == 409 and c.json()["error"] == "stakes_cancelled"
    # 不再接受报名/撤回
    e = enroll(client, gid, a["account_secret"], "e-a2")
    assert e.status_code == 409 and e.json()["error"] == "stakes_cancelled"
    w = withdraw(client, gid, a["account_secret"], "w-a")
    assert w.status_code == 409 and w.json()["error"] == "stakes_cancelled"

    # 重复取消：拒绝且不会重复退款
    again = client.post(f"/referee/games/{gid}/cancel", headers=client.headers_ref)
    assert again.status_code == 409
    assert me(client, a["account_secret"])["available_points"] == 10


def test_cancel_before_start_and_start_rejected_after(client) -> None:
    a = make_account(client, "alice", 10)
    b = make_account(client, "bob", 10)
    game = make_stakes_game(client, ["alice", "bob"], 5)
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-a")
    # 只报了一人也可取消；占用退回
    resp = client.post(f"/referee/games/{gid}/cancel", headers=client.headers_ref)
    assert resp.status_code == 200
    assert resp.json()["refunded_total"] == 5
    assert me(client, a["account_secret"])["available_points"] == 10
    # 取消后不能发车
    s = client.post(f"/referee/games/{gid}/start", headers=client.headers_ref)
    assert s.status_code == 409
    assert s.json()["error"] == "stakes_cancelled"


def test_cancel_after_finish_rejected(client) -> None:
    a = make_account(client, "solo", 10)
    game = make_stakes_game(client, ["solo"], 5, width=2, height=1, finish=[[1, 0]])
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-s")
    start(client, gid)
    tok = takeover_seat(client, gid, roster_of(game, "solo")["player_id"], a["account_secret"])["device_token"]
    client.post(f"/games/{gid}/commands", headers=device_headers(tok), json={"direction": "e"})
    close(client, gid)
    assert client.get(f"/games/{gid}").json()["status"] == "finished"

    resp = client.post(f"/referee/games/{gid}/cancel", headers=client.headers_ref)
    assert resp.status_code == 409
    assert resp.json()["error"] == "stakes_already_finished"
    # 奖金未被退回覆盖
    assert me(client, a["account_secret"])["available_points"] == 10  # 10-5+5


def test_cancel_rejected_for_normal_game(client) -> None:
    from .conftest import make_game

    game = make_game(client, players=["alice"])
    resp = client.post(
        f"/referee/games/{game['game_id']}/cancel", headers=client.headers_ref
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_a_stakes_game"


def test_normal_game_has_no_stakes_view(client) -> None:
    from .conftest import make_game

    game = make_game(client, players=["alice"])
    resp = client.get(f"/games/{game['game_id']}/stakes")
    assert resp.status_code == 404
    # 普通比赛的公开状态不含 stakes 概要
    assert client.get(f"/games/{game['game_id']}").json()["stakes"] is None


# ---------------------------------------------------------------------------
# 重启持久性：结算/退款只发生一次
# ---------------------------------------------------------------------------
def test_restart_preserves_state_and_single_settlement(client) -> None:
    from race_server.main import app

    a = make_account(client, "solo", 10)
    game = make_stakes_game(client, ["solo"], 5, width=2, height=1, finish=[[1, 0]])
    gid = game["game_id"]
    enroll(client, gid, a["account_secret"], "e-s")

    # 模拟服务重启：同一数据库上重建应用实例
    with TestClient(app) as c2:
        c2.headers_ref = client.headers_ref  # type: ignore[attr-defined]
        mine = c2.get("/accounts/me", headers=account_headers(a["account_secret"])).json()
        assert mine["available_points"] == 5
        assert len(mine["holds"]) == 1

        start(c2, gid)
        seat = stakes_state(c2, gid)["roster"][0]["player_id"]
        taken = c2.post(
            f"/games/{gid}/players/{seat}/takeover",
            headers=takeover_headers(a["account_secret"]),
        ).json()
        c2.post(
            f"/games/{gid}/commands",
            headers=device_headers(taken["device_token"]),
            json={"direction": "e"},
        )
        close(c2, gid)
        mine = c2.get("/accounts/me", headers=account_headers(a["account_secret"])).json()
        assert mine["available_points"] == 10
        payouts = [e for e in mine["ledger"] if e["kind"] == "payout"]
        assert len(payouts) == 1

    # 再次“重启”后状态依旧，结算不重复
    with TestClient(app) as c3:
        mine = c3.get("/accounts/me", headers=account_headers(a["account_secret"])).json()
        assert mine["available_points"] == 10
        payouts = [e for e in mine["ledger"] if e["kind"] == "payout"]
        assert len(payouts) == 1
        state = c3.get(f"/games/{gid}/stakes").json()
        assert state["status"] == "finished"
        assert state["settlement"]["pot"] == 5
