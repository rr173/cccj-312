"""八人双败杯测试：签表、开赛、比赛、晋级、总决赛、重赛与失效传播。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import close, device_headers, takeover_headers

REFEREE_PLAYERS = [f"p{i}" for i in range(1, 9)]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def make_account(client: TestClient, name: str) -> dict:
    resp = client.post(
        "/referee/accounts",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={"name": name, "initial_points": 0},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_cup(
    client: TestClient,
    names: list[str] | None = None,
    *,
    width: int = 3,
    height: int = 2,
    finish: list[list[int]] | None = None,
    blocked: list[list[int]] | None = None,
) -> dict:
    names = names or REFEREE_PLAYERS
    accounts = {n: make_account(client, n) for n in names}
    resp = client.post(
        "/referee/cups",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={
            "name": "cup",
            "width": width,
            "height": height,
            "blocked": blocked or [],
            "finish": finish or [[2, 0], [2, 1]],
            "turn_duration_seconds": 60,
            "seeds": names,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    body["accounts"] = accounts
    return body


def cup_state(client: TestClient, cup_id: str) -> dict:
    resp = client.get(f"/cups/{cup_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def match_view(state: dict, match_no: int) -> dict:
    return next(m for m in state["matches"] if m["match_no"] == match_no)


def start_match(client: TestClient, cup_id: str, match_no: int, request_id: str, **kw):
    payload = {"request_id": request_id}
    payload.update(kw)
    return client.post(
        f"/referee/cups/{cup_id}/matches/{match_no}/start",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json=payload,
    )


def rematch(client: TestClient, cup_id: str, game_id: str, request_id: str):
    return client.post(
        f"/referee/cups/{cup_id}/games/{game_id}/rematch",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={"request_id": request_id},
    )


def game_players(client: TestClient, game_id: str) -> dict[str, str]:
    """name -> player_id"""
    resp = client.get(f"/games/{game_id}")
    assert resp.status_code == 200, resp.text
    return {p["name"]: p["player_id"] for p in resp.json()["players"]}


def takeover(client: TestClient, game_id: str, player_id: str, secret: str) -> str:
    resp = client.post(
        f"/games/{game_id}/players/{player_id}/takeover",
        headers=takeover_headers(secret),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["device_token"]


def submit(client: TestClient, game_id: str, token: str, direction: str):
    return client.post(
        f"/games/{game_id}/commands",
        headers=device_headers(token),
        json={"direction": direction},
    )


def close_game(client: TestClient, game_id: str, expected: int | None = None):
    payload = {"expected_turn_no": expected} if expected is not None else None
    resp = client.post(
        f"/referee/games/{game_id}/close",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def play_match(
    client: TestClient,
    cup: dict,
    state: dict,
    match_no: int,
    *,
    winner_seed: int,
    tie: bool = False,
) -> str:
    """按计划打完某场次的对局，返回 game_id。

    - tie=True：两人同回合到达终点，原始签位靠前者胜（因此 winner_seed 必须是较小签位）。
    - 否则胜者两回合到达，负者四回合到达。
    """
    view = match_view(state, match_no)
    game_id = view["active_game_id"]
    assert game_id, f"场次 {match_no} 尚未开赛"
    seeds = {s["seed_no"]: s for s in state["seeds"]}
    names = game_players(client, game_id)
    home_seed = view["home"]["seed_no"]
    away_seed = view["away"]["seed_no"]
    accounts = cup["accounts"]
    tokens: dict[int, str] = {}
    for seed_no in (home_seed, away_seed):
        name = seeds[seed_no]["name"]
        pid = names[name]
        tokens[seed_no] = takeover(client, game_id, pid, accounts[name]["account_secret"])

    loser_seed = away_seed if winner_seed == home_seed else home_seed

    def do_turn(seed_dirs: dict[int, str | None]):
        for seed_no, direction in seed_dirs.items():
            if direction is not None:
                r = submit(client, game_id, tokens[seed_no], direction)
                assert r.status_code == 201, r.text
        return close_game(client, game_id)

    if tie:
        assert winner_seed == min(home_seed, away_seed)
        do_turn({home_seed: "e", away_seed: "e"})
        do_turn({home_seed: "e", away_seed: "e"})
    else:
        # 回合 1-2：胜者到达；负者等待
        do_turn({winner_seed: "e", loser_seed: "wait"})
        do_turn({winner_seed: "e", loser_seed: "wait"})
        # 回合 3-4：胜者已完赛；负者到达
        do_turn({loser_seed: "e"})
        do_turn({loser_seed: "e"})

    final = client.get(f"/games/{game_id}").json()
    assert final["status"] == "finished", final
    return game_id


def play_through_winners(client: TestClient, cup: dict, state: dict) -> tuple[dict, list[str]]:
    """打完首轮四场（签位较小者胜者），返回更新后的状态与对局 id 列表。"""
    gids = []
    for m, lower in ((1, 1), (2, 3), (3, 5), (4, 7)):
        state = cup_state(client, cup["cup_id"])
        start_match(client, cup["cup_id"], m, f"start-m{m}")
        state = cup_state(client, cup["cup_id"])
        gids.append(play_match(client, cup, state, m, winner_seed=lower))
    return cup_state(client, cup["cup_id"]), gids


# ---------------------------------------------------------------------------
# 创建：签表公布与校验
# ---------------------------------------------------------------------------
def test_cup_requires_eight_distinct_existing_accounts(client) -> None:
    names = [f"a{i}" for i in range(8)]
    for n in names:
        make_account(client, n)

    # 重复账号
    dup = client.post(
        "/referee/cups",
        headers=client.headers_ref,
        json={"finish": [[2, 0], [2, 1]], "seeds": [names[0]] * 8},
    )
    assert dup.status_code == 422
    assert dup.json()["error"] == "duplicate_seed_account"

    # 账号不存在
    missing = client.post(
        "/referee/cups",
        headers=client.headers_ref,
        json={"finish": [[2, 0], [2, 1]], "seeds": names[:7] + ["ghost"]},
    )
    assert missing.status_code == 422
    assert missing.json()["error"] == "account_not_found"

    # 必须正好八个
    seven = client.post(
        "/referee/cups",
        headers=client.headers_ref,
        json={"finish": [[2, 0], [2, 1]], "seeds": names[:7]},
    )
    assert seven.status_code == 422

    # 终点格不足 2 个（两名选手都须完赛）
    make_account(client, "z")
    one_finish = client.post(
        "/referee/cups",
        headers=client.headers_ref,
        json={"finish": [[2, 0]], "seeds": names},
    )
    assert one_finish.status_code == 422
    assert one_finish.json()["error"] == "not_enough_finish_cells"

    # 需要裁定员令牌
    noauth = client.post("/referee/cups", json={"finish": [[2, 0]], "seeds": names})
    assert noauth.status_code == 401


def test_cup_announces_full_bracket_before_start(client) -> None:
    cup = make_cup(client)
    state = cup_state(client, cup["cup_id"])

    assert [(s["seed_no"], s["name"]) for s in state["seeds"]] == [
        (i + 1, REFEREE_PLAYERS[i]) for i in range(8)
    ]
    # 首轮按相邻签位配对
    pairing = {
        m: (match_view(state, m)["home"]["seed_no"], match_view(state, m)["away"]["seed_no"])
        for m in (1, 2, 3, 4)
    }
    assert pairing == {1: (1, 2), 2: (3, 4), 3: (5, 6), 4: (7, 8)}
    # 首轮四场立即可以开赛；其余场次尚未确定选手
    assert state["startable_matches"] == [1, 2, 3, 4]
    assert match_view(state, 5)["eligible"] is False
    assert match_view(state, 5)["home"]["candidate_seeds"] == [1, 2]
    assert match_view(state, 5)["away"]["candidate_seeds"] == [3, 4]
    assert state["status"] == "setup"
    assert state["revision"] == 0
    # 创建响应也带首轮签表
    assert len(cup["first_round"]) == 4
    assert [(m["home"]["seed_no"], m["away"]["seed_no"]) for m in cup["first_round"]] == [
        (1, 2), (3, 4), (5, 6), (7, 8)
    ]


# ---------------------------------------------------------------------------
# 开赛：资格、接手、比赛
# ---------------------------------------------------------------------------
def test_start_requires_both_sides_determined_and_creates_playable_game(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]

    # 次轮场次两侧未定，不能开赛
    r = start_match(client, gid, 5, "s5")
    assert r.status_code == 409
    assert r.json()["error"] == "match_not_ready"

    # 首轮可开赛；返回对局 id
    r = start_match(client, gid, 1, "s1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_no"] == 1 and body["game_id"]
    assert body["revision"] == 1

    state = cup_state(client, gid)
    m1 = match_view(state, 1)
    assert m1["status"] == "in_progress"
    assert m1["active_game_id"] == body["game_id"]
    assert state["status"] == "active"
    # 场次 1 已有有效对局，不能重复开赛
    again = start_match(client, gid, 1, "s1b")
    assert again.status_code == 409
    assert again.json()["error"] == "match_already_started"

    # 对局是普通比赛：公开状态 active，两名选手就是对应签位账号
    game = client.get(f"/games/{body['game_id']}").json()
    assert game["status"] == "active"
    names = {p["name"] for p in game["players"]}
    assert names == {"p1", "p2"}

    # 关联对局查询
    link = client.get(f"/cups/games/{body['game_id']}").json()
    assert link["match_no"] == 1 and link["valid"] is True
    assert link["home_seed_no"] == 1 and link["away_seed_no"] == 2


def test_only_owning_account_can_take_seat(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    game_id = start_match(client, gid, 1, "s1").json()["game_id"]
    names = game_players(client, game_id)
    p1_pid = names["p1"]
    p2_pid = names["p2"]

    # 其他账号密钥不能接手
    bad = client.post(
        f"/games/{game_id}/players/{p1_pid}/takeover",
        headers=takeover_headers(cup["accounts"]["p2"]["account_secret"]),
    )
    assert bad.status_code == 401
    # 本人账号接手成功
    ok = client.post(
        f"/games/{game_id}/players/{p1_pid}/takeover",
        headers=takeover_headers(cup["accounts"]["p1"]["account_secret"]),
    )
    assert ok.status_code == 201
    other = client.post(
        f"/games/{game_id}/players/{p2_pid}/takeover",
        headers=takeover_headers(cup["accounts"]["p2"]["account_secret"]),
    )
    assert other.status_code == 201


def test_earlier_finisher_wins_and_advances(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    start_match(client, gid, 1, "s1")
    state = cup_state(client, gid)
    game_id = play_match(client, cup, state, 1, winner_seed=1)

    state = cup_state(client, gid)
    m1 = match_view(state, 1)
    assert m1["status"] == "finished"
    assert m1["winner_seed_no"] == 1
    assert m1["loser_seed_no"] == 2
    link = client.get(f"/cups/games/{game_id}").json()
    assert link["status"] == "completed"
    assert link["winner_account_id"] == cup["accounts"]["p1"]["account_id"]
    # 胜者进胜者组次轮，负者进败者组首轮
    assert match_view(state, 5)["home"]["seed_no"] == 1
    assert match_view(state, 8)["home"]["seed_no"] == 2


def test_tie_break_by_original_seed(client) -> None:
    # 两人同回合到达终点：原始签位靠前者（签位 1）胜
    cup = make_cup(client)
    gid = cup["cup_id"]
    start_match(client, gid, 1, "s1")
    state = cup_state(client, gid)
    play_match(client, cup, state, 1, winner_seed=1, tie=True)
    m1 = match_view(cup_state(client, gid), 1)
    assert m1["winner_seed_no"] == 1
    assert m1["loser_seed_no"] == 2


# ---------------------------------------------------------------------------
# 双败结构与总决赛
# ---------------------------------------------------------------------------
def test_loser_drops_to_losers_bracket_and_second_loss_eliminates(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    state, _ = play_through_winners(client, cup, cup_state(client, gid))

    # 首轮负者 2/4/6/8 进入败者组首轮
    for m, home, away in ((8, 2, 4), (9, 6, 8)):
        view = match_view(state, m)
        assert (view["home"]["seed_no"], view["away"]["seed_no"]) == (home, away)
    # 次轮场次 10/11 的一侧来自败者组首轮胜者（此时未定）
    assert match_view(state, 10)["eligible"] is False

    # 打完胜者组次轮：1 胜 3、5 胜 7
    for m, lower in ((5, 1), (6, 5)):
        start_match(client, gid, m, f"start-m{m}")
        state = cup_state(client, gid)
        play_match(client, cup, state, m, winner_seed=lower)

    # 胜者组晋级：胜者组决赛将是 1 对 5；负者 3/7 落入败者组次轮
    state = cup_state(client, gid)
    assert match_view(state, 7)["home"]["seed_no"] == 1
    assert match_view(state, 7)["away"]["seed_no"] == 5
    assert match_view(state, 10)["away"]["seed_no"] == 3
    assert match_view(state, 11)["away"]["seed_no"] == 7

    # 败者组首轮：2 胜 4、6 胜 8
    for m, lower in ((8, 2), (9, 6)):
        start_match(client, gid, m, f"start-m{m}")
        state = cup_state(client, gid)
        play_match(client, cup, state, m, winner_seed=lower)
    state = cup_state(client, gid)

    # 败者组次轮：2 对 3、6 对 7；胜者进入半决赛
    for m, winner in ((10, 2), (11, 6)):
        state = cup_state(client, gid)
        assert match_view(state, m)["eligible"] is True
        start_match(client, gid, m, f"start-m{m}")
        state = cup_state(client, gid)
        play_match(client, cup, state, m, winner_seed=winner)
    state = cup_state(client, gid)

    # 半决赛：2 对 6；胜者 2 进败者组决赛
    assert match_view(state, 12)["eligible"] is True
    start_match(client, gid, 12, "start-m12")
    state = cup_state(client, gid)
    play_match(client, cup, state, 12, winner_seed=2)

    # 胜者组决赛：1 胜 5；5 落入败者组决赛对 2
    start_match(client, gid, 7, "start-m7")
    state = cup_state(client, gid)
    play_match(client, cup, state, 7, winner_seed=1)
    state = cup_state(client, gid)
    assert match_view(state, 13)["home"]["seed_no"] == 2
    assert match_view(state, 13)["away"]["seed_no"] == 5

    # 败者组决赛：5 胜 2（2 第二次失败，淘汰）
    start_match(client, gid, 13, "start-m13")
    state = cup_state(client, gid)
    play_match(client, cup, state, 13, winner_seed=5)
    state = cup_state(client, gid)
    standings = {s["seed_no"]: s["status"] for s in state["standings"]}
    assert standings[2] == "eliminated"

    # 总决赛：胜者组冠军 1 对败者组冠军 5
    assert match_view(state, 14)["home"]["seed_no"] == 1
    assert match_view(state, 14)["away"]["seed_no"] == 5
    assert match_view(state, 14)["startable"] is True


def test_winners_champion_wins_grand_final_directly(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    # 复用完整流程到总决赛
    _drive_to_grand_final(client, cup)
    state = cup_state(client, gid)
    assert match_view(state, 14)["eligible"] is True

    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    # 胜者组选手（1）赢下总决赛即夺冠，不需要加赛
    play_match(client, cup, state, 14, winner_seed=1)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 1
    assert state["champion_name"] == "p1"
    assert state["status"] == "finished"
    assert match_view(state, 15)["eligible"] is False
    assert state["startable_matches"] == []
    assert {s["seed_no"]: s["status"] for s in state["standings"]}[1] == "champion"
    # 冠军产生后不能开新场次
    r = start_match(client, gid, 15, "late")
    assert r.status_code == 409
    assert r.json()["error"] == "cup_finished"


def test_losers_champion_winning_grand_final_forces_decider(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)

    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    # 败者组选手（5）赢下总决赛 -> 需要加赛
    play_match(client, cup, state, 14, winner_seed=5)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] is None
    assert match_view(state, 15)["eligible"] is True
    assert match_view(state, 15)["startable"] is True
    assert (match_view(state, 15)["home"]["seed_no"], match_view(state, 15)["away"]["seed_no"]) == (1, 5)

    start_match(client, gid, 15, "start-m15")
    state = cup_state(client, gid)
    # 加赛中胜者组选手赢 -> 夺冠
    play_match(client, cup, state, 15, winner_seed=1)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 1


def test_decider_can_be_won_by_losers_side(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    play_match(client, cup, state, 14, winner_seed=5)
    start_match(client, gid, 15, "start-m15")
    state = cup_state(client, gid)
    play_match(client, cup, state, 15, winner_seed=5)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 5
    assert {s["seed_no"]: s["status"] for s in state["standings"]}[1] == "runner_up"


def _drive_to_grand_final(client: TestClient, cup: dict) -> None:
    """一路打到总决赛前：胜者组冠军 1，败者组冠军 5。"""
    gid = cup["cup_id"]
    play_through_winners(client, cup, cup_state(client, gid))
    for m, lower in ((5, 1), (6, 5), (8, 2), (9, 6), (10, 2), (11, 6)):
        start_match(client, gid, m, f"start-m{m}")
        state = cup_state(client, gid)
        play_match(client, cup, state, m, winner_seed=lower)
    start_match(client, gid, 12, "start-m12")
    state = cup_state(client, gid)
    play_match(client, cup, state, 12, winner_seed=2)
    start_match(client, gid, 7, "start-m7")
    state = cup_state(client, gid)
    play_match(client, cup, state, 7, winner_seed=1)
    start_match(client, gid, 13, "start-m13")
    state = cup_state(client, gid)
    play_match(client, cup, state, 13, winner_seed=5)


# ---------------------------------------------------------------------------
# 重赛与失效传播
# ---------------------------------------------------------------------------
def test_rematch_invalidates_downstream_and_withdraws_champion(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    grand_final_game = play_match(client, cup, state, 14, winner_seed=1)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 1

    # 裁定总决赛重赛：冠军失去依据，必须撤回
    r = rematch(client, gid, grand_final_game, "rm-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invalidated_game_id"] == grand_final_game
    assert body["champion_withdrawn"] is True
    assert grand_final_game in body["invalidated_games"]

    state = cup_state(client, gid)
    assert state["champion_seed_no"] is None
    assert state["champion_account_id"] is None
    assert state["champion_withdrawn"] is True
    assert state["status"] == "active"
    m14 = match_view(state, 14)
    assert m14["startable"] is True
    assert m14["active_game_id"] is None
    assert m14["current_game_id"] is None
    assert m14["games"][-1]["game_id"] == grand_final_game
    assert m14["games"][-1]["valid"] is False
    # 失效记录可查
    inv = state["invalidations"][-1]
    assert inv["invalidated_game_id"] == grand_final_game
    assert inv["reason"] == "referee_rematch"
    assert inv["champion_withdrawn"] is True

    # 已公开公告保留
    ann = client.get(f"/games/{grand_final_game}/announcements/4")
    assert ann.status_code == 200

    # 失效对局不能再接手/提交/裁定
    names = game_players(client, grand_final_game)
    t = client.post(
        f"/games/{grand_final_game}/players/{names['p1']}/takeover",
        headers=takeover_headers(cup["accounts"]["p1"]["account_secret"]),
    )
    assert t.status_code == 409 and t.json()["error"] == "cup_game_invalidated"
    c = client.post(f"/referee/games/{grand_final_game}/close", headers=client.headers_ref)
    assert c.status_code == 409 and c.json()["error"] == "cup_game_invalidated"

    # 重新开赛：必须是新对局，不能沿用旧对局
    s = start_match(client, gid, 14, "start-m14-again")
    assert s.status_code == 200
    new_game = s.json()["game_id"]
    assert new_game != grand_final_game
    link = client.get(f"/cups/games/{new_game}").json()
    assert link["valid"] is True and link["order_index"] == 2
    old_link = client.get(f"/cups/games/{grand_final_game}").json()
    assert old_link["valid"] is False


def test_invalidation_propagates_to_all_downstream_games(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    game14 = play_match(client, cup, state, 14, winner_seed=5)
    start_match(client, gid, 15, "start-m15")
    state = cup_state(client, gid)
    game15 = play_match(client, cup, state, 15, winner_seed=1)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 1

    # 重赛总决赛第一场：加赛也依赖它 -> 一并失效
    r = rematch(client, gid, game14, "rm-14")
    assert r.status_code == 200, r.text
    assert set(r.json()["invalidated_games"]) == {game14, game15}
    state = cup_state(client, gid)
    assert state["champion_seed_no"] is None
    m15 = match_view(state, 15)
    assert m15["eligible"] is False  # 加赛不再需要
    assert [g["valid"] for g in m15["games"]] == [False]
    # 加赛对局同样拒绝裁定
    c = client.post(f"/referee/games/{game15}/close", headers=client.headers_ref)
    assert c.status_code == 409 and c.json()["error"] == "cup_game_invalidated"


def test_rematch_invalidates_in_progress_downstream_game(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    state, gids = play_through_winners(client, cup, cup_state(client, gid))
    # 场次 5（胜者组次轮 1 对 3）已开赛但尚未打完
    game5 = start_match(client, gid, 5, "start-m5").json()["game_id"]
    state = cup_state(client, gid)
    assert match_view(state, 5)["status"] == "in_progress"
    game_players(client, game5)

    # 重赛场次 1（首轮）：正在进行的场次 5 一局因依赖它而失效
    r = rematch(client, gid, gids[0], "rm-m1")
    assert r.status_code == 200, r.text
    assert game5 in r.json()["invalidated_games"]

    # 进行中的对局立即失效：不能接手/提交/裁定
    names = game_players(client, game5)
    seed_names = {s["seed_no"]: s["name"] for s in cup["seeds"]}
    p1_name = seed_names[1]
    t = client.post(
        f"/games/{game5}/players/{names[p1_name]}/takeover",
        headers=takeover_headers(cup["accounts"][p1_name]["account_secret"]),
    )
    assert t.status_code == 409 and t.json()["error"] == "cup_game_invalidated"
    c = client.post(f"/referee/games/{game5}/close", headers=client.headers_ref)
    assert c.status_code == 409 and c.json()["error"] == "cup_game_invalidated"

    state = cup_state(client, gid)
    # 场次 5 回到未开赛，需在场次 1 重开并重赛后再开新局
    assert match_view(state, 5)["status"] == "pending"
    assert match_view(state, 5)["eligible"] is False


def test_cup_never_touches_account_points(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    play_match(client, cup, state, 14, winner_seed=1)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 1

    # 不报名扣分、不发积分奖：八个账号余额始终为 0，且没有任何资金流水
    for name, acc in cup["accounts"].items():
        mine = client.get(
            "/accounts/me", headers={"X-Account-Secret": acc["account_secret"]}
        ).json()
        assert mine["available_points"] == 0, name
        assert mine["holds"] == []
        assert mine["ledger"] == [], name


def test_new_champion_after_rematch_keeps_withdrawn_history(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    game14 = play_match(client, cup, state, 14, winner_seed=1)
    assert cup_state(client, gid)["champion_seed_no"] == 1

    rematch(client, gid, game14, "rm-14")
    state = cup_state(client, gid)
    assert state["champion_seed_no"] is None and state["champion_withdrawn"] is True

    start_match(client, gid, 14, "start-m14-2")
    state = cup_state(client, gid)
    play_match(client, cup, state, 14, winner_seed=5)
    state = cup_state(client, gid)
    # 败者组选手赢下总决赛 -> 需要加赛；此阶段仍无冠军
    assert state["champion_seed_no"] is None
    start_match(client, gid, 15, "start-m15-2")
    state = cup_state(client, gid)
    play_match(client, cup, state, 15, winner_seed=5)
    state = cup_state(client, gid)
    assert state["champion_seed_no"] == 5
    # 曾撤回冠军的历史仍可查
    assert state["champion_withdrawn"] is True
    assert any(i["champion_withdrawn"] for i in state["invalidations"])


def test_rematch_of_early_game_keeps_unrelated_games(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    state, gids = play_through_winners(client, cup, cup_state(client, gid))
    # 再打次轮场次 5（1 胜 3），场次 6 尚不开赛
    start_match(client, gid, 5, "start-m5")
    state = cup_state(client, gid)
    game5 = play_match(client, cup, state, 5, winner_seed=1)

    # 重赛场次 1（首轮）：场次 5 直接依赖它 -> 失效；场次 2/3/4 及其下游保留
    r = rematch(client, gid, gids[0], "rm-m1")
    assert r.status_code == 200, r.text
    invalidated = set(r.json()["invalidated_games"])
    assert gids[0] in invalidated and game5 in invalidated
    # 不依赖场次 1 的对局原样保留
    for gid_kept in (gids[1], gids[2], gids[3]):
        assert gid_kept not in invalidated
        assert client.get(f"/cups/games/{gid_kept}").json()["valid"] is True
    state = cup_state(client, gid)
    assert match_view(state, 2)["status"] == "finished"
    assert match_view(state, 1)["status"] == "awaiting_players"
    assert match_view(state, 5)["status"] == "pending"
    # 首轮 1 重开后，场次 2 的胜者仍在等待
    s = start_match(client, gid, 1, "start-m1-again")
    assert s.status_code == 200


def test_rematch_requires_completed_game_and_cup_membership(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    game_id = start_match(client, gid, 1, "s1").json()["game_id"]
    # 未完赛不能重赛
    r = rematch(client, gid, game_id, "rm-open")
    assert r.status_code == 409
    assert r.json()["error"] == "game_not_completed"

    # 非杯赛对局
    other = client.post(
        "/referee/games",
        headers=client.headers_ref,
        json={"finish": [[2, 0]], "players": ["x", "y"]},
    ).json()
    r2 = rematch(client, gid, other["game_id"], "rm-other")
    assert r2.status_code == 404
    assert r2.json()["error"] == "not_cup_game"
    # 对局关联查询对普通比赛返回 404
    assert client.get(f"/cups/games/{other['game_id']}").status_code == 404


def test_rematch_is_idempotent_via_request_id(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    _drive_to_grand_final(client, cup)
    start_match(client, gid, 14, "start-m14")
    state = cup_state(client, gid)
    game14 = play_match(client, cup, state, 14, winner_seed=1)

    r1 = rematch(client, gid, game14, "rm-x")
    assert r1.status_code == 200 and r1.json()["idempotent_replay"] is False
    r2 = rematch(client, gid, game14, "rm-x")
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["invalidated_game_id"] == game14
    # 不能重复对同一局做新的重赛
    r3 = rematch(client, gid, game14, "rm-y")
    assert r3.status_code == 409 and r3.json()["error"] == "game_already_invalidated"
    # 请求标识不能挪作其他对局
    state = cup_state(client, gid)
    m13 = match_view(state, 13)["current_game_id"]
    r4 = rematch(client, gid, m13, "rm-x")
    assert r4.status_code == 409 and r4.json()["error"] == "request_id_conflict"


# ---------------------------------------------------------------------------
# 幂等与迟到请求
# ---------------------------------------------------------------------------
def test_start_request_is_idempotent_and_replays_original_game(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    r1 = start_match(client, gid, 1, "start-1")
    assert r1.status_code == 200
    game1 = r1.json()["game_id"]
    r2 = start_match(client, gid, 1, "start-1")
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["game_id"] == game1
    # 同一请求标识挪到场次 2 -> 冲突
    r3 = start_match(client, gid, 2, "start-1")
    assert r3.status_code == 409 and r3.json()["error"] == "request_id_conflict"
    # 同一请求标识用于重赛 -> 冲突
    r4 = rematch(client, gid, game1, "start-1")
    assert r4.status_code == 409 and r4.json()["error"] == "request_id_conflict"


def test_stale_start_request_cannot_open_a_new_game_after_rematch(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    # 第一轮比赛
    state, _ = play_through_winners(client, cup, cup_state(client, gid))
    start_match(client, gid, 5, "start-m5")
    state = cup_state(client, gid)
    game5 = play_match(client, cup, state, 5, winner_seed=1)

    # 拿到当前签表版本（此时场次 5 已完成）
    revision = cup_state(client, gid)["revision"]

    # 裁定场次 5 重赛
    rematch(client, gid, game5, "rm-m5")
    # 用重赛后最新的版本开新局
    fresh_revision = cup_state(client, gid)["revision"]
    assert fresh_revision > revision
    fresh = start_match(client, gid, 5, "start-m5-2", expected_revision=fresh_revision)
    assert fresh.status_code == 200, fresh.text
    new_game = fresh.json()["game_id"]
    assert new_game != game5

    # 迟到的旧请求（基于重赛前的旧版本）不能操作新对局
    stale = start_match(client, gid, 5, "start-m5-stale", expected_revision=revision)
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale_revision"
    # 不带版本保护的迟到开赛请求：该场次已有有效对局，同样被拒
    late = start_match(client, gid, 5, "start-m5-late")
    assert late.status_code == 409
    assert late.json()["error"] == "match_already_started"
    # 新对局仍是唯一有效对局
    state = cup_state(client, gid)
    active = [g for g in match_view(state, 5)["games"] if g["valid"]]
    assert [g["game_id"] for g in active] == [new_game]


# ---------------------------------------------------------------------------
# 重启持久性
# ---------------------------------------------------------------------------
def test_restart_preserves_bracket_and_rematch_history(client) -> None:
    from race_server.main import app

    cup = make_cup(client)
    gid = cup["cup_id"]
    state, gids = play_through_winners(client, cup, cup_state(client, gid))
    start_match(client, gid, 5, "start-m5")
    state = cup_state(client, gid)
    game5 = play_match(client, cup, state, 5, winner_seed=1)
    rematch(client, gid, gids[0], "rm-m1")
    before = cup_state(client, gid)

    with TestClient(app) as c2:
        c2.headers_ref = client.headers_ref  # type: ignore[attr-defined]
        after = c2.get(f"/cups/{gid}").json()
        assert after["invalidations"] == before["invalidations"]
        assert after["revision"] == before["revision"]
        assert [(g["game_id"], g["valid"]) for g in match_view(after, 5)["games"]] == [
            (game5, False)
        ]
        # 重赛请求重放仍返回首次结果
        r = c2.post(
            f"/referee/cups/{gid}/games/{gids[0]}/rematch",
            headers=c2.headers_ref,  # type: ignore[attr-defined]
            json={"request_id": "rm-m1"},
        )
        assert r.status_code == 200 and r.json()["idempotent_replay"] is True
        # 开赛请求重放返回原对局
        r2 = c2.post(
            f"/referee/cups/{gid}/matches/5/start",
            headers=c2.headers_ref,  # type: ignore[attr-defined]
            json={"request_id": "start-m5"},
        )
        assert r2.status_code == 200 and r2.json()["game_id"] == game5
        # 重启后仍不能凭失效资格开赛：场次 1（被重赛）与依赖它的场次 5 都不可开，
        # 不依赖场次 1 的场次 6/9 仍可开
        startable = c2.get(f"/cups/{gid}").json()["startable_matches"]
        assert 1 in startable and 5 not in startable
        assert startable == [1, 6, 9]


# ---------------------------------------------------------------------------
# 现有功能不受影响
# ---------------------------------------------------------------------------
def test_cup_games_endpoint_lists_all_games_including_invalid(client) -> None:
    cup = make_cup(client)
    gid = cup["cup_id"]
    state, gids = play_through_winners(client, cup, cup_state(client, gid))
    start_match(client, gid, 5, "start-m5")
    state = cup_state(client, gid)
    game5 = play_match(client, cup, state, 5, winner_seed=1)
    rematch(client, gid, gids[0], "rm-m1")

    rows = client.get(f"/cups/{gid}/games").json()
    by_id = {r["game_id"]: r for r in rows}
    assert set(by_id) == {*gids, game5}
    assert by_id[gids[0]]["valid"] is False
    assert by_id[game5]["valid"] is False
    assert by_id[gids[1]]["valid"] is True
    assert by_id[gids[0]]["home_seed_no"] == 1
    assert by_id[gids[0]]["away_seed_no"] == 2


def test_normal_and_stakes_games_still_work(client) -> None:
    from .conftest import make_game, start

    game = make_game(client, players=["alice"])
    start(client, game["game_id"])
    ann = close(client, game["game_id"])
    assert ann["game_status"] == "active"
