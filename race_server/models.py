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


class Announcement(BaseModel):
    game_id: str
    turn_no: int
    closed_at: str
    deadline: str
    board: dict[str, list[int]]
    results: list[MoveResult]
    winners: list[str]
    game_status: str


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
