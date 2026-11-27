"""裁定引擎纯函数测试：覆盖题目给定的全部裁定规则。"""
from __future__ import annotations

import pytest

from race_server.engine import AdjudicationError, adjudicate

BLOCKED: set[tuple[int, int]] = set()


def res(results: dict, pid: str) -> dict:
    return results[pid]


def test_wait_and_missing_command_keep_position() -> None:
    board = {"A": (0, 0), "B": (1, 0)}
    out = adjudicate(board, {"A": "wait"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 0)
    assert res(out, "A")["reason"] == "waiting"
    # B 未交指令 -> 等待
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (1, 0)
    assert res(out, "B")["reason"] == "no_command"


def test_simple_move_into_empty_succeeds() -> None:
    board = {"A": (0, 0)}
    out = adjudicate(board, {"A": "e"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (1, 0)
    assert res(out, "A")["success"] is True
    assert res(out, "A")["reason"] == "moved"


def test_two_movers_same_target_both_stay() -> None:
    # A(0,0) 与 B(0,2) 同时进入 (0,1)
    board = {"A": (0, 0), "B": (0, 2)}
    out = adjudicate(board, {"A": "s", "B": "n"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 0)
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (0, 2)
    assert res(out, "A")["reason"] == "collision_target_shared"
    assert res(out, "B")["reason"] == "collision_target_shared"


def test_swap_positions_succeeds() -> None:
    board = {"A": (0, 0), "B": (0, 1)}
    out = adjudicate(board, {"A": "s", "B": "n"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 1)
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (0, 0)
    assert all(r["success"] for r in out.values())
    assert {r["reason"] for r in out.values()} == {"moved_cycle"}


def test_four_node_cycle_succeeds() -> None:
    # 最近邻网格最短环长为 4。2x2 四环：
    # A(0,0) e->B(1,0); B s->C(1,1); C w->X(0,1); X n->A(0,0)
    board = {"A": (0, 0), "B": (1, 0), "C": (1, 1), "X": (0, 1)}
    cmds = {"A": "e", "B": "s", "C": "w", "X": "n"}
    out = adjudicate(board, cmds, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (1, 0)
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (1, 1)
    assert (res(out, "C")["x"], res(out, "C")["y"]) == (0, 1)
    assert (res(out, "X")["x"], res(out, "X")["y"]) == (0, 0)
    assert all(r["success"] for r in out.values())
    assert {r["reason"] for r in out.values()} == {"moved_cycle"}


def test_tail_feeding_into_cycle_conflicts_and_fails() -> None:
    # E(0,2) n 想进入 X 的原格 (0,1)，但环内 C 也要进入该格：
    # 两名移动者同目标 -> 规则 2，E、C 都留在原处，整环随之被堵。
    board = {"A": (0, 0), "B": (1, 0), "C": (1, 1), "X": (0, 1), "E": (0, 2)}
    cmds = {"A": "e", "B": "s", "C": "w", "X": "n", "E": "n"}
    out = adjudicate(board, cmds, BLOCKED, 5, 5)
    assert (res(out, "E")["x"], res(out, "E")["y"]) == (0, 2)
    assert res(out, "E")["reason"] == "collision_target_shared"
    assert (res(out, "C")["x"], res(out, "C")["y"]) == (1, 1)
    assert res(out, "C")["reason"] == "collision_target_shared"
    assert all(not r["success"] for r in out.values() if r["direction"] != "wait")


def test_follow_occupant_who_moves_away() -> None:
    # A->B 的格，B->空格：A 跟进成功
    board = {"A": (0, 0), "B": (1, 0)}
    out = adjudicate(board, {"A": "e", "B": "e"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (1, 0)
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (2, 0)
    assert res(out, "A")["reason"] == "moved_following_occupant"


def test_blocked_by_waiting_occupant() -> None:
    # B 原地等待（没有目标格），A 想进入 B 的格 -> 依赖链终止于等待者，A 被堵
    board = {"A": (0, 0), "B": (1, 0)}
    out = adjudicate(board, {"A": "e", "B": "wait"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 0)
    assert res(out, "A")["reason"] == "blocked_by_staying_occupant"
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (1, 0)


def test_chain_into_collision_propagates_failure() -> None:
    # 布局：A(0,0) B(1,0) C(2,0) 一横排；D(3,1)
    # D 与 C 都要进入 (3,0) -> C、D 冲突失败；
    # B 想跟进 C 但 C 没动 -> B 被堵；A 想跟进 B 也被堵。
    board = {"A": (0, 0), "B": (1, 0), "C": (2, 0), "D": (3, 1)}
    cmds = {"A": "e", "B": "e", "C": "e", "D": "n"}
    out = adjudicate(board, cmds, BLOCKED, 5, 5)
    assert (res(out, "C")["x"], res(out, "C")["y"]) == (2, 0)
    assert res(out, "C")["reason"] == "collision_target_shared"
    assert (res(out, "D")["x"], res(out, "D")["y"]) == (3, 1)
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (1, 0)
    assert res(out, "B")["reason"] == "blocked_by_staying_occupant"
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 0)
    assert res(out, "A")["reason"] == "blocked_by_staying_occupant"


def test_collision_into_waiting_cell_leaves_waiter_intact() -> None:
    board = {"A": (0, 0), "B": (0, 1)}
    out = adjudicate(board, {"A": "s", "B": "wait"}, BLOCKED, 5, 5)
    assert (res(out, "A")["x"], res(out, "A")["y"]) == (0, 0)
    assert res(out, "A")["success"] is False
    assert (res(out, "B")["x"], res(out, "B")["y"]) == (0, 1)
    assert res(out, "B")["reason"] == "waiting"


def test_blocked_cell_and_bounds_rejected() -> None:
    board = {"A": (0, 0)}
    with pytest.raises(AdjudicationError):
        adjudicate(board, {"A": "w"}, BLOCKED, 5, 5)
    with pytest.raises(AdjudicationError):
        adjudicate(board, {"A": "e"}, {(1, 0)}, 5, 5)
