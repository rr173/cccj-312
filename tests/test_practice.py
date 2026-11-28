"""练习赛大厅功能测试：房间、排队、配对邀请、确认开局、过期与提前结束。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from .conftest import close, device_headers, takeover_headers


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


def make_room(client: TestClient, **kw) -> dict:
    body = {
        "name": "lobby",
        "width": 5,
        "height": 3,
        "blocked": [],
        "finish": [[4, 0], [4, 1]],
        "turn_duration_seconds": 60,
        "confirm_timeout_seconds": 60,
    }
    body.update(kw)
    resp = client.post(
        "/referee/practice-rooms",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def ah(secret: str) -> dict[str, str]:
    return {"X-Account-Secret": secret}


def register(client: TestClient, room_id: str, acc: dict, req_id: str):
    return client.post(
        f"/practice-rooms/{room_id}/register",
        headers=ah(acc["account_secret"]),
        json={"request_id": req_id},
    )


def withdraw(client: TestClient, room_id: str, acc: dict, req_id: str):
    return client.post(
        f"/practice-rooms/{room_id}/withdraw",
        headers=ah(acc["account_secret"]),
        json={"request_id": req_id},
    )


def pair(client: TestClient, room_id: str, req_id: str, acc: dict | None = None):
    if acc is None:  # 裁定员发起
        return client.post(
            f"/referee/practice-rooms/{room_id}/pair",
            headers=client.headers_ref,  # type: ignore[attr-defined]
            json={"request_id": req_id},
        )
    return client.post(
        f"/practice-rooms/{room_id}/pair",
        headers=ah(acc["account_secret"]),
        json={"request_id": req_id},
    )


def confirm(client: TestClient, invitation_id: str, acc: dict):
    return client.post(
        f"/practice-invitations/{invitation_id}/confirm",
        headers=ah(acc["account_secret"]),
    )


def decline(client: TestClient, invitation_id: str, acc: dict):
    return client.post(
        f"/practice-invitations/{invitation_id}/decline",
        headers=ah(acc["account_secret"]),
    )


def my_lobby(client: TestClient, acc: dict) -> dict:
    resp = client.get("/practice/me", headers=ah(acc["account_secret"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def my_reg(client: TestClient, acc: dict, room_id: str) -> dict | None:
    for r in my_lobby(client, acc)["registrations"]:
        if r["room_id"] == room_id:
            return r
    return None


def room_detail(client: TestClient, room_id: str) -> dict:
    resp = client.get(f"/practice-rooms/{room_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_pair(client: TestClient, room_id: str, req_id: str = "pair-1") -> dict:
    """配对并断言成功，返回邀请视图。"""
    resp = pair(client, room_id, req_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paired"] is True
    inv = body["invitation"]
    assert inv["status"] == "pending"
    assert len(inv["seats"]) == 2
    return inv


def start_practice_game(client: TestClient, room_id: str, a: dict, b: dict) -> dict:
    """两人确认开局，返回 (confirm 响应, game_id)。"""
    inv = make_pair(client, room_id)
    r1 = confirm(client, inv["invitation_id"], a)
    assert r1.status_code == 200, r1.text
    assert r1.json()["started"] is False
    r2 = confirm(client, inv["invitation_id"], b)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["started"] is True and body["game_id"]
    return body


def seat_of(invitation: dict, account_id: str) -> dict:
    return next(s for s in invitation["seats"] if s["account_id"] == account_id)


def play_turn(client: TestClient, gid: str, tokens: list[str], direction: str = "e"):
    for tok in tokens:
        r = client.post(
            f"/games/{gid}/commands",
            headers=device_headers(tok),
            json={"direction": direction},
        )
        assert r.status_code == 201, r.text
    return close(client, gid)


# ---------------------------------------------------------------------------
# 房间开设与校验
# ---------------------------------------------------------------------------
def test_room_requires_referee_token(client) -> None:
    resp = client.post("/referee/practice-rooms", json={"finish": [[1, 0], [1, 1]]})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_room_validation(client) -> None:
    # 缺终点
    r = client.post(
        "/referee/practice-rooms",
        headers=client.headers_ref,
        json={"width": 3, "height": 2, "finish": []},
    )
    assert r.status_code == 422
    # 终点不足两格（两名选手都须完赛）
    r = client.post(
        "/referee/practice-rooms",
        headers=client.headers_ref,
        json={"width": 3, "height": 2, "finish": [[2, 0]]},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "not_enough_finish_cells"
    # 起点不足两格（左边界被堵死）
    r = client.post(
        "/referee/practice-rooms",
        headers=client.headers_ref,
        json={
            "width": 3, "height": 2,
            "blocked": [[0, 0], [0, 1]],
            "finish": [[2, 0], [2, 1]],
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "not_enough_start_cells"
    # 非法确认时限
    r = client.post(
        "/referee/practice-rooms",
        headers=client.headers_ref,
        json={"finish": [[2, 0], [2, 1]], "confirm_timeout_seconds": 0},
    )
    assert r.status_code == 422


def test_room_list_and_detail_public(client) -> None:
    room = make_room(client, name="大厅A")
    rooms = client.get("/practice-rooms").json()
    assert [r["room_id"] for r in rooms] == [room["room_id"]]
    assert rooms[0]["confirm_timeout_seconds"] == 60
    detail = room_detail(client, room["room_id"])
    assert detail["queue"] == [] and detail["games"] == []
    missing = client.get("/practice-rooms/nope")
    assert missing.status_code == 404
    assert missing.json()["error"] == "room_not_found"


# ---------------------------------------------------------------------------
# 报名与撤销
# ---------------------------------------------------------------------------
def test_register_and_queue_order(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b, c = (make_account(client, n) for n in ("a", "b", "c"))
    for i, acc in enumerate((a, b, c)):
        r = register(client, rid, acc, f"reg-{i}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"
        assert r.json()["seq"] == i + 1

    detail = room_detail(client, rid)
    assert [q["name"] for q in detail["queue"]] == ["a", "b", "c"]
    assert detail["queued"] == 3

    # 本人排队状态与位置
    lobby = my_lobby(client, b)
    reg = next(r for r in lobby["registrations"] if r["room_id"] == rid)
    assert reg["status"] == "queued" and reg["position"] == 2

    # 重复报名（新请求标识）被拒
    r = register(client, rid, b, "reg-again")
    assert r.status_code == 409
    assert r.json()["error"] == "already_registered"
    # 队列不变
    assert room_detail(client, rid)["queued"] == 3


def test_register_idempotent_replay(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a = make_account(client, "a")
    r1 = register(client, rid, a, "same-req")
    r2 = register(client, rid, a, "same-req")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["idempotent_replay"] is False
    assert r2.json()["idempotent_replay"] is True
    assert r1.json()["seq"] == r2.json()["seq"]
    # 只占一个排队名额
    assert room_detail(client, rid)["queued"] == 1
    # 同一标识挪作撤销被拒
    r = withdraw(client, rid, a, "same-req")
    assert r.status_code == 409
    assert r.json()["error"] == "request_id_conflict"


def test_withdraw_and_requeue_at_tail(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b, c = (make_account(client, n) for n in ("a", "b", "c"))
    for i, acc in enumerate((a, b, c)):
        register(client, rid, acc, f"reg-{i}")

    r = withdraw(client, rid, a, "wd-1")
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
    # 重试旧撤销：幂等返回，不影响他人
    r = withdraw(client, rid, a, "wd-1")
    assert r.json()["idempotent_replay"] is True
    # 再撤销（新标识）：没有可撤的报名
    r = withdraw(client, rid, a, "wd-2")
    assert r.status_code == 409
    assert r.json()["error"] == "not_registered"

    # 重新报名排到队尾（新序号），原有两人顺序不变
    r = register(client, rid, a, "reg-new")
    assert r.status_code == 200
    assert r.json()["epoch"] == 2
    assert r.json()["seq"] == 4
    detail = room_detail(client, rid)
    assert [q["name"] for q in detail["queue"]] == ["b", "c", "a"]

    # 重试更早的报名请求：返回首次结果，不消耗新的排队名额
    r = register(client, rid, a, "reg-0")
    assert r.status_code == 200
    assert r.json()["idempotent_replay"] is True
    assert r.json()["seq"] == 1  # 首次处理时的序号
    assert room_detail(client, rid)["queued"] == 3


def test_withdraw_requires_live_registration(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid)
    # 已收到邀请的报名不能撤销
    r = withdraw(client, rid, a, "wd-a")
    assert r.status_code == 409
    assert r.json()["error"] == "registration_invited"
    # 未报名过的账号
    c = make_account(client, "c")
    r = withdraw(client, rid, c, "wd-c")
    assert r.status_code == 409
    assert r.json()["error"] == "not_registered"


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------
def test_pair_needs_two_players(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a = make_account(client, "a")
    r = pair(client, rid, "p-0")
    assert r.status_code == 200
    assert r.json()["paired"] is False
    register(client, rid, a, "reg-a")
    r = pair(client, rid, "p-1")
    assert r.json()["paired"] is False
    # 重试同一配对请求：返回首次结果
    r = pair(client, rid, "p-1")
    assert r.json()["paired"] is False
    assert r.json()["idempotent_replay"] is True


def test_pair_takes_earliest_two_in_queue_order(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    accs = [make_account(client, n) for n in ("a", "b", "c", "d")]
    for i, acc in enumerate(accs):
        register(client, rid, acc, f"reg-{i}")
    inv = make_pair(client, rid, "p-1")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]
    # 被取中的两人变为 invited，其余仍在队列
    detail = room_detail(client, rid)
    status = {q["name"]: q["status"] for q in detail["queue"]}
    assert status == {"a": "invited", "b": "invited", "c": "queued", "d": "queued"}
    # 再配对取下一对
    inv2 = make_pair(client, rid, "p-2")
    assert [s["name"] for s in inv2["seats"]] == ["c", "d"]
    # 队列清空后不再发邀请
    r = pair(client, rid, "p-3")
    assert r.json()["paired"] is False


def test_pair_idempotent_replay_returns_same_invitation(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    r1 = pair(client, rid, "p-1")
    r2 = pair(client, rid, "p-1")
    assert r2.json()["idempotent_replay"] is True
    assert r1.json()["invitation_id"] == r2.json()["invitation_id"]
    # 只有一份邀请
    assert room_detail(client, rid)["invited"] == 2
    # 选手也可以发起配对（与裁定员同一幂等空间）
    r3 = pair(client, rid, "p-1", acc=a)
    assert r3.json()["invitation_id"] == r1.json()["invitation_id"]


def test_pair_skips_paused_and_resumes_after_resolution(client) -> None:
    r1 = make_room(client, name="R1")
    r2 = make_room(client, name="R2")
    a, b, c = (make_account(client, n) for n in ("a", "b", "c"))
    # a 同时在两个房间排队
    register(client, r1["room_id"], a, "a-r1")
    register(client, r2["room_id"], a, "a-r2")
    register(client, r1["room_id"], b, "b-r1")
    register(client, r2["room_id"], c, "c-r2")

    inv = make_pair(client, r1["room_id"], "p-1")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]
    # a 在 R2 的报名被暂停（保留原序号），配对时跳过
    reg = my_reg(client, a, r2["room_id"])
    assert reg["status"] == "paused" and reg["position"] is None
    r = pair(client, r2["room_id"], "p-2")
    assert r.json()["paired"] is False  # 只剩 c 一人可配

    # a 拒绝邀请：a 从 R1 退队，b 恢复排队，a 在 R2 的报名恢复
    r = decline(client, inv["invitation_id"], a)
    assert r.status_code == 200
    assert r.json()["status"] == "declined"
    assert my_reg(client, a, r1["room_id"])["status"] == "removed"
    assert my_reg(client, b, r1["room_id"])["status"] == "queued"
    reg = my_reg(client, a, r2["room_id"])
    assert reg["status"] == "queued" and reg["position"] == 1

    # R2 现在可以配成 a、c（a 保持原顺序在 c 前）
    inv2 = make_pair(client, r2["room_id"], "p-3")
    assert [s["name"] for s in inv2["seats"]] == ["a", "c"]


def test_decline_restores_original_queue_order(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b, c = (make_account(client, n) for n in ("a", "b", "c"))
    for i, acc in enumerate((a, b, c)):
        register(client, rid, acc, f"reg-{i}")
    inv = make_pair(client, rid, "p-1")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]
    # a 拒绝：a 退队，b 回到原位置（仍在 c 之前）
    decline(client, inv["invitation_id"], a)
    detail = room_detail(client, rid)
    assert [q["name"] for q in detail["queue"]] == ["b", "c"]
    inv2 = make_pair(client, rid, "p-2")
    assert [s["name"] for s in inv2["seats"]] == ["b", "c"]


def test_decline_idempotent_and_closed_invitation(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid)
    iid = inv["invitation_id"]
    r1 = decline(client, iid, a)
    assert r1.status_code == 200
    # 本人重试旧拒绝：幂等
    r2 = decline(client, iid, a)
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    # 已解除的邀请不能再确认/再拒绝
    assert confirm(client, iid, b).status_code == 409
    assert confirm(client, iid, b).json()["error"] == "invitation_closed"
    assert decline(client, iid, b).status_code == 409
    # 无关账号无权操作
    c = make_account(client, "c")
    r = confirm(client, iid, c)
    assert r.status_code == 403
    assert r.json()["error"] == "not_invited"


# ---------------------------------------------------------------------------
# 确认与开局
# ---------------------------------------------------------------------------
def test_confirm_starts_playable_game(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid)
    iid = inv["invitation_id"]

    # 第一人确认：尚未开局
    r = confirm(client, iid, a)
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert r.json()["all_confirmed"] is False
    # 本人重复确认：幂等，无副作用
    r = confirm(client, iid, a)
    assert r.json()["idempotent_replay"] is True
    assert r.json()["started"] is False

    # 第二人确认：直接开出一局可玩的比赛
    r = confirm(client, iid, b)
    body = r.json()
    assert body["started"] is True
    gid = body["game_id"]
    seats = body["invitation"]["seats"]
    pa = seat_of(body["invitation"], a["account_id"])["player_id"]
    pb = seat_of(body["invitation"], b["account_id"])["player_id"]
    assert pa and pb and pa != pb

    # 对局立即可玩：公开状态 active、回合 1 开放
    state = client.get(f"/games/{gid}").json()
    assert state["status"] == "active"
    assert state["current_turn_no"] == 1
    assert state["turn"]["status"] == "open"
    assert state["practice"]["room_id"] == rid
    assert state["practice"]["status"] == "active"
    positions = {p["name"]: tuple(p["position"]) for p in state["players"]}
    assert positions == {"a": (0, 0), "b": (0, 1)}

    # 双方本房报名用掉
    assert my_reg(client, a, rid)["status"] == "consumed"
    assert my_reg(client, b, rid)["status"] == "consumed"

    # 凭本人账号接手席位，沿用现有回合玩法
    ta = client.post(
        f"/games/{gid}/players/{pa}/takeover", headers=takeover_headers(a["account_secret"])
    )
    assert ta.status_code == 201, ta.text
    tb = client.post(
        f"/games/{gid}/players/{pb}/takeover", headers=takeover_headers(b["account_secret"])
    )
    assert tb.status_code == 201, tb.text
    # 他人账号不能接手
    tc = client.post(
        f"/games/{gid}/players/{pa}/takeover", headers=takeover_headers(b["account_secret"])
    )
    assert tc.status_code == 401

    # 重试旧确认：返回原对局，绝不开新局
    r = confirm(client, iid, a)
    assert r.json()["idempotent_replay"] is True
    assert r.json()["game_id"] == gid
    assert len(room_detail(client, rid)["games"]) == 1

    # 已开出的邀请不能再拒绝
    assert decline(client, iid, a).status_code == 409

    # 打两个回合验证沿用现有玩法
    play_turn(client, gid, [ta.json()["device_token"], tb.json()["device_token"]])
    state = client.get(f"/games/{gid}").json()
    positions = {p["name"]: tuple(p["position"]) for p in state["players"]}
    assert positions == {"a": (1, 0), "b": (1, 1)}


def test_game_occupies_until_finish_then_releases(client) -> None:
    r1 = make_room(client, name="R1")
    r2 = make_room(client, name="R2")
    a, b = make_account(client, "a"), make_account(client, "b")
    c, d = make_account(client, "c"), make_account(client, "d")
    # a、b 在两个房间都排队；c、d 只在 R2
    register(client, r1["room_id"], a, "a-r1")
    register(client, r1["room_id"], b, "b-r1")
    register(client, r2["room_id"], a, "a-r2")
    register(client, r2["room_id"], b, "b-r2")
    register(client, r2["room_id"], c, "c-r2")
    register(client, r2["room_id"], d, "d-r2")

    started = start_practice_game(client, r1["room_id"], a, b)
    gid = started["game_id"]
    # 开局后 a、b 在 R2 的报名继续暂停，R2 只能配出 c、d
    assert my_reg(client, a, r2["room_id"])["status"] == "paused"
    inv2 = make_pair(client, r2["room_id"], "p-r2")
    assert [s["name"] for s in inv2["seats"]] == ["c", "d"]
    # c、d 也确认开局后，R2 无可配之人
    confirm(client, inv2["invitation_id"], c)
    confirm(client, inv2["invitation_id"], d)
    assert pair(client, r2["room_id"], "p-r2-b").json()["paired"] is False

    # a、b 完赛（各自向东 4 步到达 (4,0)/(4,1)）
    inv_view = client.get(
        f"/practice-invitations/{started['invitation']['invitation_id']}",
        headers=ah(a["account_secret"]),
    ).json()
    pa = seat_of(inv_view, a["account_id"])["player_id"]
    pb = seat_of(inv_view, b["account_id"])["player_id"]
    ta = client.post(
        f"/games/{gid}/players/{pa}/takeover", headers=takeover_headers(a["account_secret"])
    ).json()["device_token"]
    tb = client.post(
        f"/games/{gid}/players/{pb}/takeover", headers=takeover_headers(b["account_secret"])
    ).json()["device_token"]
    for _ in range(4):
        ann = play_turn(client, gid, [ta, tb])
    assert ann["game_status"] == "finished"

    # 全员完赛：练习对局 finished，双方在 R2 的暂停报名恢复原顺序
    games = my_lobby(client, a)["games"]
    assert next(g for g in games if g["game_id"] == gid)["status"] == "finished"
    reg = my_reg(client, a, r2["room_id"])
    assert reg["status"] == "queued" and reg["position"] == 1
    assert my_reg(client, b, r2["room_id"])["status"] == "queued"
    # 公开对局状态
    state = client.get(f"/games/{gid}").json()
    assert state["status"] == "finished"
    assert state["practice"]["status"] == "finished"


# ---------------------------------------------------------------------------
# 过期（以服务端时间为准，懒处理）
# ---------------------------------------------------------------------------
def test_expired_invitation_removes_unconfirmed_and_unblocks_pairing(client) -> None:
    room = make_room(client, confirm_timeout_seconds=1)
    rid = room["room_id"]
    a, b, c, d = (make_account(client, n) for n in ("a", "b", "c", "d"))
    for i, acc in enumerate((a, b, c, d)):
        register(client, rid, acc, f"reg-{i}")
    inv = make_pair(client, rid, "p-1")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]

    time.sleep(2.2)  # 等邀请过期（服务端时间判定）

    # 迟到的确认不能复活过期邀请
    r = confirm(client, inv["invitation_id"], a)
    assert r.status_code == 409
    assert r.json()["error"] == "invitation_expired"
    # 双方都未确认：都从该房退队
    assert my_reg(client, a, rid)["status"] == "removed"
    assert my_reg(client, b, rid)["status"] == "removed"
    # 过期邀请不挡后续配对：c、d 立刻可配
    inv2 = make_pair(client, rid, "p-2")
    assert [s["name"] for s in inv2["seats"]] == ["c", "d"]


def test_expiry_keeps_confirmed_player_in_queue(client) -> None:
    room = make_room(client, confirm_timeout_seconds=1)
    rid = room["room_id"]
    a, b, c = (make_account(client, n) for n in ("a", "b", "c"))
    for i, acc in enumerate((a, b, c)):
        register(client, rid, acc, f"reg-{i}")
    inv = make_pair(client, rid, "p-1")
    # a 在时限内确认，b 迟迟不确认
    r = confirm(client, inv["invitation_id"], a)
    assert r.status_code == 200

    time.sleep(2.2)

    # b 迟到的确认无效
    assert confirm(client, inv["invitation_id"], b).status_code == 409
    # 到期未配成：仅未确认的 b 退队；a 保留原排队顺序
    reg_a = my_reg(client, a, rid)
    assert reg_a["status"] == "queued" and reg_a["position"] == 1
    assert my_reg(client, b, rid)["status"] == "removed"
    # a 无须重新排队，直接参与下一次配对
    inv2 = make_pair(client, rid, "p-2")
    assert [s["name"] for s in inv2["seats"]] == ["a", "c"]


def test_expired_invitation_view_and_me_listing(client) -> None:
    room = make_room(client, confirm_timeout_seconds=1)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid, "p-1")
    iid = inv["invitation_id"]
    # 过期前：待回复邀请可见
    assert [i["invitation_id"] for i in my_lobby(client, a)["invitations"]] == [iid]
    time.sleep(2.2)
    # 过期后：不再出现在待回复列表，查询状态为 expired
    assert my_lobby(client, a)["invitations"] == []
    view = client.get(f"/practice-invitations/{iid}", headers=ah(a["account_secret"]))
    assert view.json()["status"] == "expired"


# ---------------------------------------------------------------------------
# 提前结束
# ---------------------------------------------------------------------------
def test_end_practice_early(client) -> None:
    r1 = make_room(client, name="R1")
    r2 = make_room(client, name="R2")
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, r1["room_id"], a, "a-r1")
    register(client, r1["room_id"], b, "b-r1")
    register(client, r2["room_id"], a, "a-r2")
    register(client, r2["room_id"], b, "b-r2")
    started = start_practice_game(client, r1["room_id"], a, b)
    gid = started["game_id"]
    pa = seat_of(started["invitation"], a["account_id"])["player_id"]
    pb = seat_of(started["invitation"], b["account_id"])["player_id"]
    ta = client.post(
        f"/games/{gid}/players/{pa}/takeover", headers=takeover_headers(a["account_secret"])
    ).json()["device_token"]

    # 先打一个回合，留下已公开公告
    ann = play_turn(client, gid, [ta])
    assert ann["turn_no"] == 1

    # 裁定员提前结束：不算完赛
    r = client.post(
        f"/referee/practice-games/{gid}/end", headers=client.headers_ref
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ended"
    assert set(body["released_accounts"]) == {a["account_id"], b["account_id"]}

    # 已公开公告保留；不再接手、提交或裁定新回合
    kept = client.get(f"/games/{gid}/announcements/1")
    assert kept.status_code == 200
    assert kept.json()["turn_no"] == 1
    state = client.get(f"/games/{gid}").json()
    assert state["status"] == "ended"
    assert state["practice"]["status"] == "ended"
    r = client.post(
        f"/games/{gid}/players/{pb}/takeover", headers=takeover_headers(b["account_secret"])
    )
    assert r.status_code == 409
    assert r.json()["error"] == "practice_ended"
    r = client.post(
        f"/games/{gid}/commands",
        headers=device_headers(ta),
        json={"direction": "e"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "practice_ended"
    r = client.post(f"/referee/games/{gid}/close", headers=client.headers_ref)
    assert r.status_code == 409
    assert r.json()["error"] == "practice_ended"
    # 重复结束被拒
    r = client.post(f"/referee/practice-games/{gid}/end", headers=client.headers_ref)
    assert r.status_code == 409
    assert r.json()["error"] == "practice_already_ended"

    # 双方在其他房间的暂停报名恢复，可再次配对
    assert my_reg(client, a, r2["room_id"])["status"] == "queued"
    inv = make_pair(client, r2["room_id"], "p-r2")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]


def test_end_requires_practice_game(client) -> None:
    r = client.post("/referee/practice-games/nope/end", headers=client.headers_ref)
    assert r.status_code == 404
    # 普通比赛不能按练习赛结束
    from .conftest import make_game

    game = make_game(client, players=["x", "y"])
    r = client.post(
        f"/referee/practice-games/{game['game_id']}/end", headers=client.headers_ref
    )
    assert r.status_code == 404
    assert r.json()["error"] == "practice_not_found"


# ---------------------------------------------------------------------------
# 与其他玩法互不干扰
# ---------------------------------------------------------------------------
def test_practice_does_not_touch_points(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a = make_account(client, "a", 50)
    b = make_account(client, "b", 70)
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    start_practice_game(client, rid, a, b)
    for acc, pts in ((a, 50), (b, 70)):
        me = client.get("/accounts/me", headers=ah(acc["account_secret"])).json()
        assert me["available_points"] == pts
        # 只有初始积分流水，练习赛不产生任何积分变动
        assert [e["kind"] for e in me["ledger"]] == (["initial"] if pts else [])


def test_stakes_and_normal_games_do_not_block_lobby(client) -> None:
    # 积分赛占用积分但不占用大厅配对资格
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a", 100), make_account(client, "b", 100)
    sg = client.post(
        "/referee/stakes-games",
        headers=client.headers_ref,
        json={
            "width": 3, "height": 1, "finish": [[2, 0]],
            "roster": ["a", "b"], "entry_fee": 10,
        },
    ).json()
    r = client.post(
        f"/games/{sg['game_id']}/stakes/enroll",
        headers=ah(a["account_secret"]),
        json={"request_id": "e-1"},
    )
    assert r.status_code == 200
    # a 在积分赛有占用，仍可直接排队并配对
    r = register(client, rid, a, "reg-a")
    assert r.json()["status"] == "queued"
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid, "p-1")
    assert [s["name"] for s in inv["seats"]] == ["a", "b"]


def test_auth_errors(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    bad = {"X-Account-Secret": "wrong"}
    assert client.post(
        f"/practice-rooms/{rid}/register", headers=bad, json={"request_id": "x"}
    ).status_code == 401
    assert client.get("/practice/me", headers=bad).status_code == 401
    assert client.post("/practice-invitations/x/confirm", headers=bad).status_code == 401
    # 不存在的房间/邀请
    a = make_account(client, "a")
    assert register(client, "nope", a, "r").status_code == 404
    assert confirm(client, "nope", a).status_code == 404


# ---------------------------------------------------------------------------
# 重启持久性
# ---------------------------------------------------------------------------
def test_state_survives_restart(client, monkeypatch) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid, "p-1")
    iid = inv["invitation_id"]
    confirm(client, iid, a)

    # 模拟服务重启：同一数据库文件重新挂载应用
    from race_server.main import app

    with TestClient(app) as c2:
        # 排队、邀请状态仍可查询
        detail = c2.get(f"/practice-rooms/{rid}").json()
        assert detail["invited"] == 2
        lobby = c2.get("/practice/me", headers=ah(a["account_secret"])).json()
        assert [i["invitation_id"] for i in lobby["invitations"]] == [iid]
        # 接续办理：第二人确认照常开局
        r = c2.post(f"/practice-invitations/{iid}/confirm", headers=ah(b["account_secret"]))
        assert r.status_code == 200, r.text
        assert r.json()["started"] is True
        gid = r.json()["game_id"]
        # 重试旧报名不消耗新名额
        r = c2.post(
            f"/practice-rooms/{rid}/register",
            headers=ah(a["account_secret"]),
            json={"request_id": "reg-a"},
        )
        assert r.json()["idempotent_replay"] is True
        assert c2.get(f"/practice-rooms/{rid}").json()["active_games"] == 1
        state = c2.get(f"/games/{gid}").json()
        assert state["status"] == "active"


# ---------------------------------------------------------------------------
# 补充边界：占用中报名、撤销暂停报名、用掉后重排、子接口
# ---------------------------------------------------------------------------
def test_register_while_occupied_starts_paused(client) -> None:
    r1 = make_room(client, name="R1")
    r2 = make_room(client, name="R2")
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, r1["room_id"], a, "a-r1")
    register(client, r1["room_id"], b, "b-r1")
    inv = make_pair(client, r1["room_id"], "p-1")
    # a 持有待确认邀请时在 R2 报名：入队但暂停配对
    r = register(client, r2["room_id"], a, "a-r2")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    # 邀请解除后自动恢复
    decline(client, inv["invitation_id"], b)
    assert my_reg(client, a, r2["room_id"])["status"] == "queued"


def test_withdraw_paused_registration(client) -> None:
    r1 = make_room(client, name="R1")
    r2 = make_room(client, name="R2")
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, r1["room_id"], a, "a-r1")
    register(client, r1["room_id"], b, "b-r1")
    register(client, r2["room_id"], a, "a-r2")
    inv = make_pair(client, r1["room_id"], "p-1")
    assert my_reg(client, a, r2["room_id"])["status"] == "paused"
    # 暂停中的报名尚未收到邀请，可随时撤掉
    r = withdraw(client, r2["room_id"], a, "wd-r2")
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
    # 邀请解除不影响已撤销的报名
    decline(client, inv["invitation_id"], a)
    assert my_reg(client, a, r2["room_id"])["status"] == "withdrawn"


def test_requeue_after_registration_consumed(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    started = start_practice_game(client, rid, a, b)
    gid = started["game_id"]
    # 报名被用掉后（对局仍在进行）可以再排队：先入队但暂停
    r = register(client, rid, a, "reg-a-2")
    assert r.json()["status"] == "paused"
    assert r.json()["epoch"] == 2
    # 裁定员提前结束对局后，新报名恢复排队
    client.post(f"/referee/practice-games/{gid}/end", headers=client.headers_ref)
    reg = my_reg(client, a, rid)
    assert reg["status"] == "queued" and reg["position"] == 1


def test_me_sub_endpoints(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    inv = make_pair(client, rid, "p-1")
    regs = client.get("/practice/me/registrations", headers=ah(a["account_secret"]))
    assert regs.status_code == 200
    assert [r["status"] for r in regs.json()] == ["invited"]
    invs = client.get("/practice/me/invitations", headers=ah(a["account_secret"]))
    assert [i["invitation_id"] for i in invs.json()] == [inv["invitation_id"]]
    assert invs.json()[0]["my_response"] == "pending"
    games = client.get("/practice/me/games", headers=ah(a["account_secret"]))
    assert games.json() == []
    # 开局后 games 列出本人席位
    confirm(client, inv["invitation_id"], a)
    started = confirm(client, inv["invitation_id"], b).json()
    games = client.get("/practice/me/games", headers=ah(a["account_secret"])).json()
    assert len(games) == 1
    assert games[0]["game_id"] == started["game_id"]
    assert games[0]["status"] == "active"
    assert games[0]["my_player_id"] == seat_of(
        started["invitation"], a["account_id"]
    )["player_id"]


def test_public_practice_game_view(client) -> None:
    room = make_room(client)
    rid = room["room_id"]
    a, b = make_account(client, "a"), make_account(client, "b")
    register(client, rid, a, "reg-a")
    register(client, rid, b, "reg-b")
    started = start_practice_game(client, rid, a, b)
    gid = started["game_id"]
    view = client.get(f"/practice-games/{gid}").json()
    assert view["room_id"] == rid
    assert view["invitation_id"] == started["invitation"]["invitation_id"]
    assert view["status"] == "active"
    assert {p["name"] for p in view["players"]} == {"a", "b"}
    # 非练习赛对局
    from .conftest import make_game

    normal = make_game(client, players=["x", "y"])
    r = client.get(f"/practice-games/{normal['game_id']}")
    assert r.status_code == 404
    assert r.json()["error"] == "practice_not_found"
