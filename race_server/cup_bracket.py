"""八人双败签表结构（纯函数，不接触数据库）。

签位固定为 1..8，首轮按相邻签位配对：(1,2) (3,4) (5,6) (7,8)。
签表结构在创建时确定，重赛不重新抽签。

场次编号（拓扑序）::

    胜者组 1..4  首轮    5..6  次轮    7  胜者组决赛
    败者组 8..9  首轮    10..11 次轮   12 半决赛   13 败者组决赛
    总决赛 14；若败者组选手赢下总决赛，则需第 15 场加赛定冠军。

场次结果用 ``(winner_seed, loser_seed)`` 表示；未决出时为 ``None``。
"""
from __future__ import annotations

from typing import Any

Side = tuple[str, int]  # ("seed" | "winner" | "loser", 场次/签位号)

# match_no -> 元数据。home/away 为两侧资格来源。
BRACKET: dict[int, dict[str, Any]] = {
    1: {"bracket": "winners", "round_no": 1, "label": "胜者组首轮 1",
        "home": ("seed", 1), "away": ("seed", 2)},
    2: {"bracket": "winners", "round_no": 1, "label": "胜者组首轮 2",
        "home": ("seed", 3), "away": ("seed", 4)},
    3: {"bracket": "winners", "round_no": 1, "label": "胜者组首轮 3",
        "home": ("seed", 5), "away": ("seed", 6)},
    4: {"bracket": "winners", "round_no": 1, "label": "胜者组首轮 4",
        "home": ("seed", 7), "away": ("seed", 8)},
    5: {"bracket": "winners", "round_no": 2, "label": "胜者组次轮 1",
        "home": ("winner", 1), "away": ("winner", 2)},
    6: {"bracket": "winners", "round_no": 2, "label": "胜者组次轮 2",
        "home": ("winner", 3), "away": ("winner", 4)},
    7: {"bracket": "winners", "round_no": 3, "label": "胜者组决赛",
        "home": ("winner", 5), "away": ("winner", 6)},
    8: {"bracket": "losers", "round_no": 1, "label": "败者组首轮 1",
        "home": ("loser", 1), "away": ("loser", 2)},
    9: {"bracket": "losers", "round_no": 1, "label": "败者组首轮 2",
        "home": ("loser", 3), "away": ("loser", 4)},
    10: {"bracket": "losers", "round_no": 2, "label": "败者组次轮 1",
         "home": ("winner", 8), "away": ("loser", 5)},
    11: {"bracket": "losers", "round_no": 2, "label": "败者组次轮 2",
         "home": ("winner", 9), "away": ("loser", 6)},
    12: {"bracket": "losers", "round_no": 3, "label": "败者组半决赛",
         "home": ("winner", 10), "away": ("winner", 11)},
    13: {"bracket": "losers", "round_no": 4, "label": "败者组决赛",
         "home": ("winner", 12), "away": ("loser", 7)},
    14: {"bracket": "final", "round_no": 1, "label": "总决赛",
         "home": ("winner", 7), "away": ("winner", 13)},
    15: {"bracket": "decider", "round_no": 1, "label": "总决赛加赛",
         "home": ("loser", 14), "away": ("winner", 14)},
}

MATCH_ORDER: list[int] = list(range(1, 16))
DECIDER_MATCH = 15

Results = dict[int, tuple[int, int] | None]


def side_value(side: Side, results: Results) -> int | None:
    kind, ref = side
    if kind == "seed":
        return ref
    res = results.get(ref)
    if res is None:
        return None
    return res[0] if kind == "winner" else res[1]


def _raw_participants(match_no: int, results: Results) -> tuple[int | None, int | None]:
    """不考虑加赛规则时，场次两侧的签位。"""
    d = BRACKET[match_no]
    return side_value(d["home"], results), side_value(d["away"], results)


def decider_needed(results: Results) -> bool:
    """败者组选手赢下总决赛时，需要第 15 场加赛。"""
    res14 = results.get(14)
    if res14 is None:
        return False
    _, away14 = _raw_participants(14, results)
    if away14 is None:
        return False
    return res14[0] == away14


def participants(match_no: int, results: Results) -> tuple[int | None, int | None]:
    """场次两侧已确定的签位；任一侧未定或加赛不需要时返回 None。"""
    if match_no == DECIDER_MATCH:
        if not decider_needed(results):
            return None, None
        res14 = results[14]
        assert res14 is not None
        return res14[1], res14[0]  # 胜者组冠军（总决赛负者）为 home
    return _raw_participants(match_no, results)


def downstream(match_no: int) -> list[int]:
    """直接或间接依赖该场次结果的后续场次（不含自身）。"""
    seen: set[int] = set()
    frontier = [match_no]
    while frontier:
        cur = frontier.pop()
        for m, d in BRACKET.items():
            if m in seen or m == match_no:
                continue
            if any(
                kind in ("winner", "loser") and ref == cur
                for kind, ref in (d["home"], d["away"])
            ):
                seen.add(m)
                frontier.append(m)
    return sorted(seen)


def champion_seed(results: Results) -> int | None:
    """当前有效签表下的冠军签位；尚未决出时返回 None。"""
    res14 = results.get(14)
    if res14 is None:
        return None
    _, away14 = _raw_participants(14, results)
    if away14 is not None and res14[0] == away14:
        res15 = results.get(15)
        return res15[0] if res15 else None
    return res14[0]
