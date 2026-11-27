"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Direction = Literal["n", "s", "e", "w", "wait"]


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------
class CreateGameRequest(BaseModel):
    name: str = Field(default="race", max_length=80)
    width: int = Field(default=10, ge=1, le=64)
    height: int = Field(default=10, ge=1, le=64)
    # 阻挡格 [x, y]
    blocked: list[list[int]] = Field(default_factory=list)
    # 终点格 [x, y]
    finish: list[list[int]] = Field(default_factory=list)
    turn_duration_seconds: int = Field(default=60, ge=1, le=86400)
    players: list[str] = Field(min_length=1, max_length=16)


class JoinRequest(BaseModel):
    name: str = Field(max_length=80)


class TakeoverRequest(BaseModel):
    pass


class CloseTurnRequest(BaseModel):
    # 裁定员期望关闭的回合号；并发重复关回合时用于幂等保护
    expected_turn_no: int | None = None


class CommandRequest(BaseModel):
    direction: Direction


# ---------------------------------------------------------------------------
# 积分赛：账号 / 报名
# ---------------------------------------------------------------------------
class CreateAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # 非负整数初始积分
    initial_points: int = Field(ge=0)


class CreateStakesGameRequest(BaseModel):
    name: str = Field(default="stakes-race", max_length=80)
    width: int = Field(default=10, ge=1, le=64)
    height: int = Field(default=10, ge=1, le=64)
    blocked: list[list[int]] = Field(default_factory=list)
    finish: list[list[int]] = Field(default_factory=list)
    turn_duration_seconds: int = Field(default=60, ge=1, le=86400)
    # 预设参赛账号名单（账号名）
    roster: list[str] = Field(min_length=1, max_length=16)
    # 统一正整数报名费
    entry_fee: int = Field(ge=1)


class StakeActionRequest(BaseModel):
    # 请求标识：报名/撤回按此标识幂等重试
    request_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------
class PlayerCredential(BaseModel):
    player_id: str
    name: str
    takeover_secret: str
    device_token: str
    device_generation: int


class CreateGameResponse(BaseModel):
    game_id: str
    status: str
    current_turn_no: int
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    turn_duration_seconds: int
    players: list[PlayerCredential]


class GameSummary(BaseModel):
    game_id: str
    name: str
    status: str
    current_turn_no: int
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]


class TakeoverResponse(BaseModel):
    game_id: str
    player_id: str
    device_token: str
    device_generation: int
    old_generation: int
    current_turn_no: int
    turn_status: str
    # 本设备当前生效的指令（没有则为 None），并告知能否继续替换
    accepted_direction: Direction | None
    can_replace: bool


class CommandAcceptedResponse(BaseModel):
    game_id: str
    turn_no: int
    direction: Direction
    sequence: int
    device_generation: int
    replaceable: bool


class MoveResult(BaseModel):
    player_id: str
    x: int
    y: int
    direction: Direction
    success: bool
    reason: str
    finished: bool


# ---------------------------------------------------------------------------
# 积分赛：结算与查询
# ---------------------------------------------------------------------------
class StakesPayout(BaseModel):
    account_id: str
    player_id: str
    amount: int


class StakesSettlement(BaseModel):
    pot: int
    entry_fee: int
    # 最早到达终点的同回合选手（账号 ID 升序，余点也按此顺序发放）
    winners: list[str]
    payouts: list[StakesPayout]


class StakesSummary(BaseModel):
    status: str
    entry_fee: int
    enrolled: int
    roster_size: int
    pot: int


class Announcement(BaseModel):
    game_id: str
    turn_no: int
    closed_at: str
    deadline: str
    board: dict[str, list[int]]
    results: list[MoveResult]
    winners: list[str]
    game_status: str
    # 仅积分赛最终回合的公告携带分账结果
    stakes: StakesSettlement | None = None


class PlayerState(BaseModel):
    player_id: str
    name: str
    position: list[int] | None
    finished: bool
    finished_turn: int | None


class TurnState(BaseModel):
    turn_no: int
    status: str
    started_at: str
    deadline: str
    closed_at: str | None


class PublicGameState(BaseModel):
    game_id: str
    name: str
    status: str
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    current_turn_no: int
    turn: TurnState | None
    # 仅含上一回合已公告的棋盘（当前回合的提交进度不公开）
    board: dict[str, list[int]]
    players: list[PlayerState]
    winners: list[str]
    # 积分赛概要；普通比赛为 None
    stakes: StakesSummary | None = None


class MyPlayerState(BaseModel):
    player_id: str
    name: str
    position: list[int] | None
    finished: bool
    device_generation: int
    current_turn_no: int
    turn_status: str
    # 本回合当前生效指令及是否仍可替换；重连后据此确认指令状态
    accepted_direction: Direction | None
    can_replace: bool
    replaceable_reason: str


class RefereePlayerCommand(BaseModel):
    player_id: str
    name: str
    direction: Direction | None
    sequence: int | None
    device_generation: int | None


class RefereeTurnDetail(BaseModel):
    game_id: str
    turn_no: int
    status: str
    started_at: str
    deadline: str
    closed_at: str | None
    commands: list[RefereePlayerCommand]


class ErrorResponse(BaseModel):
    error: str
    detail: str


# ---------------------------------------------------------------------------
# 积分赛：账号与报名响应
# ---------------------------------------------------------------------------
class AccountCreated(BaseModel):
    account_id: str
    name: str
    # 账号密钥仅创建时返回一次，数据库只存哈希
    account_secret: str
    available_points: int


class AccountSummary(BaseModel):
    account_id: str
    name: str
    available_points: int
    created_at: str


class StakesRosterMember(BaseModel):
    account_id: str
    name: str
    player_id: str


class StakesGameCreated(BaseModel):
    game_id: str
    status: str
    stakes_status: str
    entry_fee: int
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    turn_duration_seconds: int
    roster: list[StakesRosterMember]


class EnrollmentResult(BaseModel):
    game_id: str
    account_id: str
    status: str
    epoch: int
    entry_fee: int
    available_points: int
    request_id: str
    # 按请求标识重试时为 true，不重复占用/返还
    idempotent_replay: bool


class RefundEntry(BaseModel):
    account_id: str
    amount: int


class CancelResult(BaseModel):
    game_id: str
    status: str
    cancelled_at: str
    refunded: list[RefundEntry]
    refunded_total: int


class HoldInfo(BaseModel):
    game_id: str
    game_name: str
    entry_fee: int
    enrolled_at: str


class LedgerEntry(BaseModel):
    entry_id: int
    kind: str
    game_id: str | None
    amount: int
    balance_after: int
    note: str | None
    created_at: str


class AccountMe(BaseModel):
    account_id: str
    name: str
    available_points: int
    # 各场当前占用（仍冻结的报名费）
    holds: list[HoldInfo]
    # 全部收支流水
    ledger: list[LedgerEntry]


class StakesRosterStatus(BaseModel):
    account_id: str
    name: str
    player_id: str
    enrollment: str


class StakesState(BaseModel):
    game_id: str
    game_status: str
    status: str
    entry_fee: int
    pot: int
    enrolled: int
    roster_size: int
    roster: list[StakesRosterStatus]
    # 终局分账结果；未结算/已取消为 None
    settlement: StakesSettlement | None
    cancelled_at: str | None
