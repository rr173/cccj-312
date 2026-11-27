"""回合裁定引擎（纯函数，不接触数据库）。

裁定规则（严格按题意）：
1. 以回合开始时的棋盘（每个玩家的起点）为准；
2. 多名玩家要进入同一个目标格 -> 这些玩家全部留在原处；
3. 其余移动仅当目标格原本为空，或原占用者最终也移动成功时才成功；
4. 互换位置（2-环）和更长的闭环都允许：整条环上的移动全部成功；
5. 未交指令与 direction=wait 一律视为等待。

实现：先处理目标格冲突（规则 2），其余玩家构成“人 -> 目标格占用者”的
依赖图，每个节点出度至多为 1；用显式栈的 DFS 判定每条依赖链，
链上出现环则环内全部成功，结果沿链反向传播。
"""
from __future__ import annotations

from typing import Any

DIRECTIONS: dict[str, tuple[int, int]] = {
    "n": (0, -1),
    "s": (0, 1),
    "e": (1, 0),
    "w": (-1, 0),
}


def neighbor(x: int, y: int, direction: str) -> tuple[int, int]:
    if direction == "wait":
        return x, y
    dx, dy = DIRECTIONS[direction]
    return x + dx, y + dy


class AdjudicationError(ValueError):
    """输入指令非法（越界、进入阻挡格、非相邻格等）。"""


def adjudicate(
    start_board: dict[str, tuple[int, int]],
    commands: dict[str, str],
    blocked: set[tuple[int, int]],
    width: int,
    height: int,
) -> dict[str, dict[str, Any]]:
    """对单回合进行裁定。

    参数
    ----
    start_board:
        回合开始时棋盘，player_id -> (x, y)。
    commands:
        本回合**最终生效**的指令集合（每玩家至多一条，即其最后一个被接受的版本）；
        未交指令的玩家不出现在字典中，按等待处理。
    blocked / width / height:
        静态地形与边界。

    返回
    ----
    player_id -> {x, y, direction, success, reason}，覆盖所有参与玩家。
    """
    players = set(start_board)
    # 格 -> 该格在开始棋盘上的占用者（一格至多一人）
    occupant: dict[tuple[int, int], str] = {pos: pid for pid, pos in start_board.items()}

    targets: dict[str, tuple[int, int]] = {}
    direction_of: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 1. 计算目标格，同时做防御性校验（正常路径下提交时已校验过）。
    #    等待者（含未交指令者）不产生“要进入的格”，不参与目标格统计。
    # ------------------------------------------------------------------
    for pid in players:
        direction = commands.get(pid, "wait")
        direction_of[pid] = direction
        x, y = start_board[pid]
        if direction == "wait":
            continue
        tx, ty = neighbor(x, y, direction)
        if not (0 <= tx < width and 0 <= ty < height):
            raise AdjudicationError(f"玩家 {pid} 的移动越界: ({tx}, {ty})")
        if (tx, ty) in blocked:
            raise AdjudicationError(f"玩家 {pid} 的目标格 ({tx}, {ty}) 为阻挡格")
        targets[pid] = (tx, ty)

    # ------------------------------------------------------------------
    # 2. 目标格冲突：多名移动者要进同一格，全部留在原处。
    #    等待者不“要进入”任何格，不参与计数；进入等待者格的情形
    #    在依赖解析阶段按规则 3（占用者未移动）处理。
    # ------------------------------------------------------------------
    target_count: dict[tuple[int, int], int] = {}
    for cell in targets.values():
        target_count[cell] = target_count.get(cell, 0) + 1

    collision_failed: set[str] = set()
    for pid, cell in targets.items():
        if target_count[cell] > 1:
            collision_failed.add(pid)

    # ------------------------------------------------------------------
    # 3. 构建移动依赖：mover -> 目标格原占用者（空目标无依赖）
    #    等待者与冲突失败者不参与依赖图。
    # ------------------------------------------------------------------
    dependency: dict[str, str] = {}
    for pid in players:
        if pid in collision_failed or direction_of[pid] == "wait":
            continue
        occ = occupant.get(targets[pid])
        if occ is not None and occ != pid:
            dependency[pid] = occ

    success: dict[str, bool] = {}
    reason: dict[str, str] = {}

    for pid in collision_failed:
        success[pid] = False
        reason[pid] = "collision_target_shared"
    for pid in players:
        if direction_of[pid] == "wait":
            success[pid] = True  # 等待“成功”，位置不变
            reason[pid] = "waiting" if pid in commands else "no_command"

    # ------------------------------------------------------------------
    # 4. DFS 解析每条依赖链（显式栈，无递归深度问题）
    #    遍历所有真正发起移动的玩家；目标为空者没有依赖，直接成功。
    # ------------------------------------------------------------------
    movers = [
        pid
        for pid in players
        if pid not in collision_failed and direction_of[pid] != "wait"
    ]
    for root in movers:
        if root in success:
            continue
        stack: list[str] = [root]
        on_stack: set[str] = {root}
        while stack:
            cur = stack[-1]
            if cur in success:
                stack.pop()
                on_stack.discard(cur)
                continue
            dep = dependency.get(cur)
            if dep is None:
                # 目标格原本为空
                success[cur] = True
                reason[cur] = "moved"
                stack.pop()
                on_stack.discard(cur)
                continue
            if dep in success:
                # 依赖者结果已知。仅当依赖者本人也是成功的移动者时，
                # 它才会腾出该格；等待者与冲突失败者都不会离开。
                dep_vacates = success[dep] and dep in targets
                if dep_vacates:
                    success[cur] = True
                    reason[cur] = "moved_following_occupant"
                else:
                    success[cur] = False
                    reason[cur] = "blocked_by_staying_occupant"
                stack.pop()
                on_stack.discard(cur)
                continue
            if dep in on_stack:
                # 发现闭环：找到环起点，环上所有移动成功（互换/长环允许）
                cycle_start = stack.index(dep)
                cycle = stack[cycle_start:]
                for node in cycle:
                    success[node] = True
                    reason[node] = "moved_cycle"
                for node in cycle:
                    stack.pop()
                    on_stack.discard(node)
                continue
            stack.append(dep)
            on_stack.add(dep)

    # ------------------------------------------------------------------
    # 5. 汇总结果（等待者位置不变）
    # ------------------------------------------------------------------
    results: dict[str, dict[str, Any]] = {}
    for pid in players:
        if pid in targets and success[pid]:
            nx, ny = targets[pid]
        else:
            nx, ny = start_board[pid]
        results[pid] = {
            "x": nx,
            "y": ny,
            "direction": direction_of[pid],
            "success": success[pid],
            "reason": reason[pid],
        }
    return results
