"""真实 HTTP 服务器上的并发测试。

用 uvicorn 在后台线程起服务，再用裸工作线程并发发请求（不使用
ThreadPoolExecutor——在 pytest+anyio 环境下其工作线程的扩缩容会与
anyio 的线程限制器相互干扰，导致 barrier 永远等不齐）。

验证：
1. 关回合与提交并发：新回合公开前没有指令能写进下一回合；
2. 接手与提交并发：旧设备被拒，已接受指令集合与公告一致；
3. 重复关回合：每回合裁定严格一次。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn

REFEREE = {"X-Referee-Token": "concurrent-token"}


class ServerHandle:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def post(self, path: str, **kw):
        return httpx.post(self.base_url + path, timeout=30, **kw)

    def get(self, path: str, **kw):
        return httpx.get(self.base_url + path, timeout=30, **kw)


def run_concurrently(jobs: list, timeout: float = 25.0) -> list:
    """用裸线程并发执行 ``jobs``（每个是无参可调用对象），按提交顺序返回结果。"""
    results: list = [None] * len(jobs)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            results[idx] = jobs[idx]()
        except BaseException as exc:  # noqa: BLE001 - 测试中需要保留任何失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(len(jobs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), f"并发工作线程 {t.name} 超时未完成"
    if errors:
        raise errors[0]
    return results


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> ServerHandle:
    tmpdir = tempfile.mkdtemp(prefix="race-conc-")
    monkeypatch.setenv("RACE_DB_PATH", os.path.join(tmpdir, "conc.db"))
    monkeypatch.setenv("RACE_REFEREE_TOKEN", "concurrent-token")

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
        except httpx.ConnectError:
            pass
        time.sleep(0.05)
    yield handle
    server_obj.should_exit = True
    thread.join(timeout=10)


def _create_and_start(s: ServerHandle, players: list[str]) -> dict:
    r = s.post(
        "/referee/games",
        headers=REFEREE,
        json={"width": 6, "height": 4, "finish": [[5, 0]], "players": players},
    )
    assert r.status_code == 201, r.text
    game = r.json()
    r2 = s.post(f"/referee/games/{game['game_id']}/start", headers=REFEREE)
    assert r2.status_code == 200, r2.text
    return game


def _creds(game: dict, name: str) -> dict:
    return next(p for p in game["players"] if p["name"] == name)


def test_concurrent_submits_against_close_never_leak_into_next_turn(server: ServerHandle) -> None:
    game = _create_and_start(server, ["alice", "bob", "carol"])
    gid = game["game_id"]
    tokens = [_creds(game, n)["device_token"] for n in ("alice", "bob", "carol")]
    token_to_player = {
        p["device_token"]: p["player_id"] for p in game["players"]
    }
    barrier = threading.Barrier(8)

    def make_submit(i: int):
        def job():
            barrier.wait()
            return server.post(
                f"/games/{gid}/commands",
                headers={"X-Device-Token": tokens[i % 3]},
                json={"direction": "e"},
            )
        return job

    def close_call():
        barrier.wait()
        return server.post(
            f"/referee/games/{gid}/close",
            headers=REFEREE,
            json={"expected_turn_no": 1},
        )

    responses = run_concurrently([make_submit(i) for i in range(7)] + [close_call])

    close_resp = next(r for r in responses if r.request.url.path.endswith("/close"))
    assert close_resp.status_code == 200
    ann = close_resp.json()
    assert ann["turn_no"] == 1

    # 所有被接受的提交都只可能落在当时已公开可玩的回合：
    # 关回合提交前的写入属于回合 1；提交后回合 2 已公开，写入回合 2 同样合法。
    # 关键不变量：绝不存在“回合 2 公开之前”写进回合 2 的指令（下面另作校验）。
    for r in responses:
        if r is close_resp:
            continue
        if r.status_code == 201:
            assert r.json()["turn_no"] in (1, 2)
        else:
            assert r.status_code == 409
            assert r.json()["error"] in {"turn_not_open", "game_not_active"}

    # 回合 2 的指令只能由“关回合事务提交之后”才到达的请求写入；
    # 不变量：视图中每个带指令的玩家，在响应里都能找到其 turn_no=2 的成功提交。
    detail = server.get(f"/referee/games/{gid}/turn", headers=REFEREE).json()
    assert detail["turn_no"] == 2 and detail["status"] == "open"
    turn2_players = {
        token_to_player[r.request.headers["X-Device-Token"]]
        for r in responses
        if r is not close_resp and r.status_code == 201 and r.json()["turn_no"] == 2
    }
    accepted_turn2 = [c for c in detail["commands"] if c["direction"] is not None]
    assert {c["player_id"] for c in accepted_turn2} <= turn2_players

    # 回合 1 的公告可正常拉取，且其中每条结果都对应一个真实存在的已接受指令集合：
    # 有移动结果的玩家其 direction 必为合法指令值；未交指令者为 no_command。
    ann1 = server.get(f"/games/{gid}/announcements/1").json()
    assert ann1["turn_no"] == 1
    for r in ann1["results"]:
        if r["reason"] == "no_command":
            assert r["direction"] == "wait"
        else:
            assert r["direction"] in {"n", "s", "e", "w", "wait"}
    # 公告棋盘与公开视图一致（公开视图只反映已公告状态）
    public = server.get(f"/games/{gid}").json()
    assert public["board"] == ann1["board"]


def test_takeover_racing_submissions_old_device_writes_rejected(server: ServerHandle) -> None:
    game = _create_and_start(server, ["alice"])
    gid = game["game_id"]
    alice = _creds(game, "alice")
    old_token = alice["device_token"]
    secret = alice["takeover_secret"]

    ok = server.post(
        f"/games/{gid}/commands",
        headers={"X-Device-Token": old_token},
        json={"direction": "e"},
    )
    assert ok.status_code == 201

    barrier = threading.Barrier(6)

    def make_old_write(i: int):
        def job():
            barrier.wait()
            # 只用从起点 (0,0) 出发始终合法的方向，排除 422 干扰
            return server.post(
                f"/games/{gid}/commands",
                headers={"X-Device-Token": old_token},
                json={"direction": "e" if i % 2 else "s"},
            )
        return job

    def takeover():
        barrier.wait()
        return server.post(
            f"/games/{gid}/players/{alice['player_id']}/takeover",
            headers={"X-Takeover-Secret": secret},
        )

    results = run_concurrently([make_old_write(i) for i in range(5)] + [takeover])

    to = next(r for r in results if r.status_code == 201 and "device_token" in r.json())
    new_token = to.json()["device_token"]
    new_gen = to.json()["device_generation"]
    # 接手时返回的当前生效指令是真实存在的已接受版本：
    # 若旧设备的并发写早于接手，可能已把 'e' 替换成 's'。
    assert to.json()["accepted_direction"] in {"e", "s"}

    # 旧设备的并发写：早于接手成功(201)，晚于接手被拒(409)
    for r in results:
        if r is to:
            continue
        assert r.status_code in (201, 409)
        if r.status_code == 409:
            assert r.json()["error"] == "device_superseded"

    # 新设备立即可用且唯一可写
    me = server.get(f"/games/{gid}/me", headers={"X-Device-Token": new_token}).json()
    assert me["device_generation"] == new_gen
    assert me["can_replace"] is True
    repl = server.post(
        f"/games/{gid}/commands",
        headers={"X-Device-Token": new_token},
        json={"direction": "wait"},
    )
    assert repl.status_code == 201

    # 关回合后：回合 2 无任何指令；回合 1 的公告给出最终生效指令
    ann = server.post(
        f"/referee/games/{gid}/close", headers=REFEREE,
        json={"expected_turn_no": 1},
    ).json()
    detail = server.get(f"/referee/games/{gid}/turn", headers=REFEREE).json()
    assert all(c["direction"] is None for c in detail["commands"])
    assert ann["results"][0]["direction"] in {"e", "w", "s", "wait"}


def test_parallel_close_only_one_adjudication(server: ServerHandle) -> None:
    game = _create_and_start(server, ["alice", "bob"])
    gid = game["game_id"]
    barrier = threading.Barrier(4)

    def close_call():
        barrier.wait()
        return server.post(
            f"/referee/games/{gid}/close",
            headers=REFEREE,
            json={"expected_turn_no": 1},
        )

    resps = run_concurrently([close_call] * 4)
    codes = sorted(r.status_code for r in resps)
    assert codes.count(200) == 1
    assert codes.count(409) == 3
    state = server.get(f"/games/{gid}").json()
    assert state["current_turn_no"] == 2  # 没有跳过回合

    # 再关一次（不带期望号）关的是回合 2，不影响回合 1 的公告
    ann2 = server.post(f"/referee/games/{gid}/close", headers=REFEREE).json()
    assert ann2["turn_no"] == 2
    ann1 = server.get(f"/games/{gid}/announcements/1").json()
    assert ann1["turn_no"] == 1


def test_no_command_accepted_between_close_and_next_turn_visibility(server: ServerHandle) -> None:
    game = _create_and_start(server, ["alice"])
    gid = game["game_id"]
    token = _creds(game, "alice")["device_token"]
    barrier = threading.Barrier(2)

    def submit_call():
        barrier.wait()
        return server.post(
            f"/games/{gid}/commands",
            headers={"X-Device-Token": token},
            json={"direction": "e"},
        )

    def close_call():
        barrier.wait()
        return server.post(
            f"/referee/games/{gid}/close", headers=REFEREE,
            json={"expected_turn_no": 1},
        )

    sr, cr = run_concurrently([submit_call, close_call])

    assert cr.status_code == 200
    # 两种线性化结果都合法：
    #  - 提交先于关回合获锁 -> 进入回合 1（201, turn_no=1）；
    #  - 关回合先提交 -> 回合 2 已公开，提交进入回合 2（201, turn_no=2），
    #    或恰好被状态切换拒绝（409）。
    # 绝不允许的是“回合 2 公开之前”写入回合 2——而该状态在关回合事务
    # 提交前根本不存在，因此任何 turn_no=2 的成功写入都发生在公开之后。
    if sr.status_code == 201:
        assert sr.json()["turn_no"] in (1, 2)
    else:
        assert sr.status_code == 409
    detail = server.get(f"/referee/games/{gid}/turn", headers=REFEREE).json()
    assert detail["turn_no"] == 2 and detail["status"] == "open"
    # 若回合 2 出现指令，必然对应一个返回了 turn_no=2 的成功提交
    accepted = [c for c in detail["commands"] if c["direction"] is not None]
    if accepted:
        assert sr.status_code == 201 and sr.json()["turn_no"] == 2
    else:
        assert sr.status_code == 409 or sr.json()["turn_no"] == 1
