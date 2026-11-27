"""积分赛并发测试（真实 uvicorn 服务器 + 裸线程并发请求）。

验证：
1. 跨场同时报名不会超用同一份积分；
2. 同一请求标识的并发报名只占用一次；
3. 取消与最终裁定并发时只有一个终局，状态与资金处理一致。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest
import uvicorn

from .test_concurrency import ServerHandle, run_concurrently

REFEREE = {"X-Referee-Token": "stakes-conc-token"}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> ServerHandle:
    tmpdir = tempfile.mkdtemp(prefix="race-stakes-conc-")
    monkeypatch.setenv("RACE_DB_PATH", os.path.join(tmpdir, "conc.db"))
    monkeypatch.setenv("RACE_REFEREE_TOKEN", "stakes-conc-token")

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


def _make_account(s: ServerHandle, name: str, points: int) -> dict:
    r = s.post(
        "/referee/accounts",
        headers=REFEREE,
        json={"name": name, "initial_points": points},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_stakes_game(s: ServerHandle, roster: list[str], fee: int, **kw) -> dict:
    body = {
        "width": 3, "height": 1, "finish": [[2, 0]],
        "turn_duration_seconds": 60, "roster": roster, "entry_fee": fee,
    }
    body.update(kw)
    r = s.post("/referee/stakes-games", headers=REFEREE, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _enroll(s: ServerHandle, gid: str, secret: str, req_id: str):
    return s.post(
        f"/games/{gid}/stakes/enroll",
        headers={"X-Account-Secret": secret},
        json={"request_id": req_id},
    )


def _me(s: ServerHandle, secret: str) -> dict:
    r = s.get("/accounts/me", headers={"X-Account-Secret": secret})
    assert r.status_code == 200, r.text
    return r.json()


def test_cross_game_concurrent_enroll_never_overspends(server: ServerHandle) -> None:
    # 余额 10，两场报名费各 6：并发报名最多成功一场
    acc = _make_account(server, "alice", 10)
    other = _make_account(server, "other", 100)
    g1 = _make_stakes_game(server, ["alice", "other"], 6)
    g2 = _make_stakes_game(server, ["alice", "other"], 6)
    secret = acc["account_secret"]

    barrier = threading.Barrier(9)

    def make_enroll(gid: str, i: int):
        def job():
            barrier.wait()
            return _enroll(server, gid, secret, f"enroll-{gid[:6]}-{i}")
        return job

    jobs = [make_enroll(g1["game_id"], i) for i in range(4)]
    jobs += [make_enroll(g2["game_id"], i) for i in range(4, 8)]

    def align_only():
        barrier.wait()
        return None

    jobs.append(align_only)  # 第 9 方仅用于对齐栅栏
    responses = run_concurrently(jobs)
    responses = [r for r in responses if r is not None]

    ok = [r for r in responses if r.status_code == 200]
    assert len(ok) == 1  # 只有一场能报上
    for r in responses:
        if r.status_code == 409:
            assert r.json()["error"] in {"insufficient_points", "already_enrolled"}
    mine = _me(server, secret)
    assert mine["available_points"] == 4
    assert len(mine["holds"]) == 1
    holds = [e for e in mine["ledger"] if e["kind"] == "hold"]
    assert len(holds) == 1 and holds[0]["amount"] == -6


def test_concurrent_enroll_same_request_id_holds_once(server: ServerHandle) -> None:
    acc = _make_account(server, "alice", 50)
    game = _make_stakes_game(server, ["alice"], 7)
    gid = game["game_id"]
    secret = acc["account_secret"]

    barrier = threading.Barrier(8)

    def job():
        barrier.wait()
        return _enroll(server, gid, secret, "same-request-id")

    responses = run_concurrently([job] * 8)
    assert all(r.status_code == 200 for r in responses)
    replays = [r for r in responses if r.json()["idempotent_replay"]]
    assert len(replays) == 7  # 恰好一个原始处理，其余都是重放
    mine = _me(server, secret)
    assert mine["available_points"] == 43
    holds = [e for e in mine["ledger"] if e["kind"] == "hold"]
    assert len(holds) == 1


def test_cancel_racing_final_close_has_exactly_one_terminal_outcome(server: ServerHandle) -> None:
    # 单人积分赛：最后一个回合的关回合（触发结算）与取消并发
    acc = _make_account(server, "solo", 10)
    # 宽 2：从 (0,0) 一步即达终点 (1,0)，本次关回合必然触发全员完赛
    game = _make_stakes_game(server, ["solo"], 5, width=2, height=1, finish=[[1, 0]])
    gid = game["game_id"]
    secret = acc["account_secret"]
    assert _enroll(server, gid, secret, "e-solo").status_code == 200
    r = server.post(f"/referee/games/{gid}/start", headers=REFEREE)
    assert r.status_code == 200, r.text
    seat = server.get(f"/games/{gid}/stakes").json()["roster"][0]["player_id"]
    taken = server.post(
        f"/games/{gid}/players/{seat}/takeover",
        headers={"X-Takeover-Secret": secret},
    ).json()
    r = server.post(
        f"/games/{gid}/commands",
        headers={"X-Device-Token": taken["device_token"]},
        json={"direction": "e"},
    )
    assert r.status_code == 201

    barrier = threading.Barrier(2)

    def close_call():
        barrier.wait()
        return server.post(f"/referee/games/{gid}/close", headers=REFEREE)

    def cancel_call():
        barrier.wait()
        return server.post(f"/referee/games/{gid}/cancel", headers=REFEREE)

    close_resp, cancel_resp = run_concurrently([close_call, cancel_call])
    codes = sorted([close_resp.status_code, cancel_resp.status_code])
    assert codes == [200, 409]  # 只有一个终局

    state = server.get(f"/games/{gid}/stakes").json()
    public = server.get(f"/games/{gid}").json()
    mine = _me(server, secret)
    payouts = [e for e in mine["ledger"] if e["kind"] == "payout"]
    refunds = [e for e in mine["ledger"] if e["kind"] == "refund"]

    if close_resp.status_code == 200:
        # 结算生效：状态 finished、公告带分账、只发一次奖、无退款
        assert close_resp.json()["game_status"] == "finished"
        assert close_resp.json()["stakes"]["pot"] == 5
        assert state["status"] == "finished" and public["status"] == "finished"
        assert state["settlement"]["payouts"][0]["amount"] == 5
        assert len(payouts) == 1 and not refunds
        assert cancel_resp.json()["error"] == "stakes_already_finished"
    else:
        # 取消生效：状态 cancelled、只退一次款、无奖金
        assert cancel_resp.json()["refunded_total"] == 5
        assert state["status"] == "cancelled" and public["status"] == "cancelled"
        assert state["settlement"] is None
        assert len(refunds) == 1 and not payouts
        assert close_resp.json()["error"] == "stakes_cancelled"
    # 两种终局下余额都回到 10（5 报名费或退回或作为奖金返还）
    assert mine["available_points"] == 10
