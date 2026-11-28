"""练习赛大厅并发测试（真实 uvicorn 服务器 + 裸线程并发请求）。

验证：
1. 并发配对同一房间不会多发有效邀请；
2. 并发确认只开一局，重复确认幂等；
3. 确认与拒绝并发只有一个结果生效；
4. 提前结束与最终关回合并发只有一个终局，双方只被放回一次；
5. 并发同标识报名只占一个排队名额。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest
import uvicorn

from .test_concurrency import ServerHandle, run_concurrently

REFEREE = {"X-Referee-Token": "practice-conc-token"}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> ServerHandle:
    tmpdir = tempfile.mkdtemp(prefix="race-practice-conc-")
    monkeypatch.setenv("RACE_DB_PATH", os.path.join(tmpdir, "conc.db"))
    monkeypatch.setenv("RACE_REFEREE_TOKEN", "practice-conc-token")

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


def _make_account(s: ServerHandle, name: str, points: int = 0) -> dict:
    r = s.post(
        "/referee/accounts",
        headers=REFEREE,
        json={"name": name, "initial_points": points},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_room(s: ServerHandle, **kw) -> dict:
    body = {
        "width": 5, "height": 3, "finish": [[4, 0], [4, 1]],
        "turn_duration_seconds": 60, "confirm_timeout_seconds": 60,
    }
    body.update(kw)
    r = s.post("/referee/practice-rooms", headers=REFEREE, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _ah(secret: str) -> dict[str, str]:
    return {"X-Account-Secret": secret}


def _register(s: ServerHandle, rid: str, acc: dict, req_id: str):
    return s.post(
        f"/practice-rooms/{rid}/register",
        headers=_ah(acc["account_secret"]),
        json={"request_id": req_id},
    )


def _pair(s: ServerHandle, rid: str, req_id: str):
    return s.post(
        f"/referee/practice-rooms/{rid}/pair",
        headers=REFEREE,
        json={"request_id": req_id},
    )


def _confirm(s: ServerHandle, iid: str, acc: dict):
    return s.post(
        f"/practice-invitations/{iid}/confirm", headers=_ah(acc["account_secret"])
    )


def _decline(s: ServerHandle, iid: str, acc: dict):
    return s.post(
        f"/practice-invitations/{iid}/decline", headers=_ah(acc["account_secret"])
    )


def _room(s: ServerHandle, rid: str) -> dict:
    r = s.get(f"/practice-rooms/{rid}")
    assert r.status_code == 200, r.text
    return r.json()


def _lobby(s: ServerHandle, acc: dict) -> dict:
    r = s.get("/practice/me", headers=_ah(acc["account_secret"]))
    assert r.status_code == 200, r.text
    return r.json()


def test_concurrent_pairing_issues_at_most_one_invitation_per_pair(server: ServerHandle) -> None:
    room = _make_room(server)
    rid = room["room_id"]
    a, b = _make_account(server, "a"), _make_account(server, "b")
    _register(server, rid, a, "reg-a")
    _register(server, rid, b, "reg-b")

    barrier = threading.Barrier(6)

    def make_pair(i: int):
        def job():
            barrier.wait()
            return _pair(server, rid, f"pair-{i}")
        return job

    responses = run_concurrently([make_pair(i) for i in range(6)])
    paired = [r for r in responses if r.json()["paired"]]
    assert len(paired) == 1  # 只有最早一请求配成，其余看到不足两人
    invitation_ids = {r.json()["invitation_id"] for r in paired}
    assert len(invitation_ids) == 1
    detail = _room(server, rid)
    assert detail["invited"] == 2 and detail["queued"] == 0


def test_concurrent_pairing_distinct_pairs_no_overlap(server: ServerHandle) -> None:
    room = _make_room(server)
    rid = room["room_id"]
    accs = [_make_account(server, f"p{i}") for i in range(4)]
    for i, acc in enumerate(accs):
        _register(server, rid, acc, f"reg-{i}")

    barrier = threading.Barrier(2)

    def make_pair(i: int):
        def job():
            barrier.wait()
            return _pair(server, rid, f"pair-{i}")
        return job

    r1, r2 = run_concurrently([make_pair(0), make_pair(1)])
    assert r1.json()["paired"] and r2.json()["paired"]
    seats1 = {s["account_id"] for s in r1.json()["invitation"]["seats"]}
    seats2 = {s["account_id"] for s in r2.json()["invitation"]["seats"]}
    assert seats1.isdisjoint(seats2)  # 一人一份待确认邀请
    assert len(seats1 | seats2) == 4


def test_concurrent_confirms_start_exactly_one_game(server: ServerHandle) -> None:
    room = _make_room(server)
    rid = room["room_id"]
    a, b = _make_account(server, "a"), _make_account(server, "b")
    _register(server, rid, a, "reg-a")
    _register(server, rid, b, "reg-b")
    iid = _pair(server, rid, "pair-1").json()["invitation_id"]

    barrier = threading.Barrier(6)

    def make_confirm(acc: dict):
        def job():
            barrier.wait()
            return _confirm(server, iid, acc)
        return job

    # 双方确认 + 各自的重试同时到达
    jobs = [make_confirm(a), make_confirm(a), make_confirm(a),
            make_confirm(b), make_confirm(b), make_confirm(b)]
    responses = run_concurrently(jobs)
    assert all(r.status_code == 200 for r in responses)
    game_ids = {r.json()["game_id"] for r in responses if r.json()["started"]}
    assert len(game_ids) == 1  # 只开一局
    detail = _room(server, rid)
    assert len(detail["games"]) == 1
    assert detail["games"][0]["status"] == "active"


def test_confirm_racing_decline_has_exactly_one_outcome(server: ServerHandle) -> None:
    room = _make_room(server)
    rid = room["room_id"]
    a, b = _make_account(server, "a"), _make_account(server, "b")
    _register(server, rid, a, "reg-a")
    _register(server, rid, b, "reg-b")
    iid = _pair(server, rid, "pair-1").json()["invitation_id"]
    # a 先确认，之后 b 的确认与拒绝并发：只有一个生效
    assert _confirm(server, iid, a).status_code == 200

    barrier = threading.Barrier(2)

    def confirm_b():
        barrier.wait()
        return _confirm(server, iid, b)

    def decline_b():
        barrier.wait()
        return _decline(server, iid, b)

    cr, dr = run_concurrently([confirm_b, decline_b])
    codes = sorted([cr.status_code, dr.status_code])
    assert codes == [200, 409]
    detail = _room(server, rid)
    games = detail["games"]
    if cr.status_code == 200:
        # 确认先生效：开出一局；拒绝迟到被拒
        assert len(games) == 1 and games[0]["status"] == "active"
        assert dr.json()["error"] == "invitation_closed"
    else:
        # 拒绝先生效：邀请解除，不开局；确认被拒
        assert games == []
        assert cr.json()["error"] == "invitation_closed"
        # b 从该房退队，a 恢复排队
        regs_a = {r["room_id"]: r["status"] for r in _lobby(server, a)["registrations"]}
        assert regs_a[rid] == "queued"
        regs_b = {r["room_id"]: r["status"] for r in _lobby(server, b)["registrations"]}
        assert regs_b[rid] == "removed"


def test_end_racing_final_close_has_exactly_one_terminal_outcome(server: ServerHandle) -> None:
    # 两人从 (0,0)/(0,1) 出发，终点 (1,0)/(1,1)：一次关回合即全员完赛
    room = _make_room(server, width=2, height=2, finish=[[1, 0], [1, 1]])
    rid = room["room_id"]
    a, b = _make_account(server, "a"), _make_account(server, "b")
    _register(server, rid, a, "reg-a")
    _register(server, rid, b, "reg-b")
    iid = _pair(server, rid, "pair-1").json()["invitation_id"]
    _confirm(server, iid, a)
    started = _confirm(server, iid, b).json()
    gid = started["game_id"]
    seats = started["invitation"]["seats"]
    tokens = []
    for seat, acc in ((seats[0], a), (seats[1], b)):
        t = server.post(
            f"/games/{gid}/players/{seat['player_id']}/takeover",
            headers={"X-Takeover-Secret": acc["account_secret"]},
        ).json()["device_token"]
        tokens.append(t)
        r = server.post(
            f"/games/{gid}/commands",
            headers={"X-Device-Token": t},
            json={"direction": "e"},
        )
        assert r.status_code == 201

    barrier = threading.Barrier(2)

    def close_call():
        barrier.wait()
        return server.post(f"/referee/games/{gid}/close", headers=REFEREE)

    def end_call():
        barrier.wait()
        return server.post(f"/referee/practice-games/{gid}/end", headers=REFEREE)

    close_resp, end_resp = run_concurrently([close_call, end_call])
    codes = sorted([close_resp.status_code, end_resp.status_code])
    assert codes == [200, 409]  # 只有一个终局

    state = server.get(f"/games/{gid}").json()
    practice = server.get(f"/practice-games/{gid}").json()
    if close_resp.status_code == 200:
        # 全员完赛生效：finished；提前结束被拒
        assert close_resp.json()["game_status"] == "finished"
        assert state["status"] == "finished" and practice["status"] == "finished"
        assert end_resp.json()["error"] == "practice_already_finished"
    else:
        # 提前结束生效：ended；最终关回合被拒
        assert end_resp.json()["status"] == "ended"
        assert state["status"] == "ended" and practice["status"] == "ended"
        assert close_resp.json()["error"] == "practice_ended"
    # 两种终局下双方都不再有占用（本房报名已用掉，无其他暂停报名残留）
    for acc in (a, b):
        lobby = _lobby(server, acc)
        assert all(r["status"] != "paused" for r in lobby["registrations"])


def test_concurrent_register_same_request_id_uses_one_slot(server: ServerHandle) -> None:
    room = _make_room(server)
    rid = room["room_id"]
    a = _make_account(server, "a")

    barrier = threading.Barrier(8)

    def job():
        barrier.wait()
        return _register(server, rid, a, "same-req")

    responses = run_concurrently([job] * 8)
    assert all(r.status_code == 200 for r in responses)
    replays = [r for r in responses if r.json()["idempotent_replay"]]
    assert len(replays) == 7  # 恰好一个原始处理
    seqs = {r.json()["seq"] for r in responses}
    assert len(seqs) == 1
    assert _room(server, rid)["queued"] == 1


def test_expired_invitation_never_revived_by_late_confirm_race(server: ServerHandle) -> None:
    room = _make_room(server, confirm_timeout_seconds=1)
    rid = room["room_id"]
    a, b = _make_account(server, "a"), _make_account(server, "b")
    c, d = _make_account(server, "c"), _make_account(server, "d")
    for i, acc in enumerate((a, b, c, d)):
        _register(server, rid, acc, f"reg-{i}")
    iid = _pair(server, rid, "pair-1").json()["invitation_id"]
    time.sleep(2.2)  # 邀请已过期

    barrier = threading.Barrier(3)

    def confirm_a():
        barrier.wait()
        return _confirm(server, iid, a)

    def confirm_b():
        barrier.wait()
        return _confirm(server, iid, b)

    def pair_next():
        barrier.wait()
        return _pair(server, rid, "pair-2")

    ra, rb, rp = run_concurrently([confirm_a, confirm_b, pair_next])
    # 迟到的确认一律无效，绝不开局
    assert ra.status_code == 409 and ra.json()["error"] == "invitation_expired"
    assert rb.status_code == 409 and rb.json()["error"] == "invitation_expired"
    assert _room(server, rid)["games"] == []
    # 过期不挡后续配对：c、d 配成
    assert rp.json()["paired"] is True
    assert {s["name"] for s in rp.json()["invitation"]["seats"]} == {"c", "d"}
