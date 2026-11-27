"""HTTP 路由层：只做鉴权、参数绑定与错误映射。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from . import config, services
from .models import (
    Announcement,
    CloseTurnRequest,
    CommandAcceptedResponse,
    CommandRequest,
    CreateGameRequest,
    CreateGameResponse,
    GameSummary,
    MyPlayerState,
    PublicGameState,
    RefereeTurnDetail,
    TakeoverResponse,
)
from .services import ServiceError

referee_router = APIRouter(prefix="/referee/games", tags=["referee"])
device_router = APIRouter(tags=["device"])
public_router = APIRouter(tags=["public"])


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------
def require_referee(x_referee_token: str | None = Header(default=None)) -> None:
    if not config.referee_token_matches(x_referee_token):
        raise ServiceError(401, "unauthorized", "缺少或错误的裁定员令牌")


# ---------------------------------------------------------------------------
# 裁定员接口
# ---------------------------------------------------------------------------
@referee_router.post(
    "", response_model=CreateGameResponse, status_code=201,
    dependencies=[Depends(require_referee)],
)
def create_game(body: CreateGameRequest) -> dict:
    return services.create_game(body)


@referee_router.get("", response_model=list[GameSummary], dependencies=[Depends(require_referee)])
def games_list() -> list[dict]:
    return services.list_games()


@referee_router.post("/{game_id}/start", dependencies=[Depends(require_referee)])
def start(game_id: str) -> dict:
    return services.start_game(game_id)


@referee_router.get(
    "/{game_id}/turn", response_model=RefereeTurnDetail,
    dependencies=[Depends(require_referee)],
)
def turn_detail(game_id: str) -> dict:
    return services.referee_turn_detail(game_id)


@referee_router.post(
    "/{game_id}/close", response_model=Announcement,
    dependencies=[Depends(require_referee)],
)
def close(game_id: str, body: CloseTurnRequest | None = None) -> dict:
    expected = body.expected_turn_no if body is not None else None
    return services.close_turn(game_id, expected)["announcement"]


@public_router.get(
    "/games/{game_id}/announcements/{turn_no}", response_model=Announcement
)
def announcement(game_id: str, turn_no: int) -> dict:
    return services.get_announcement(game_id, turn_no)


# ---------------------------------------------------------------------------
# 玩家设备接口
# ---------------------------------------------------------------------------
@device_router.post(
    "/games/{game_id}/players/{player_id}/takeover",
    response_model=TakeoverResponse,
    status_code=201,
)
def takeover(game_id: str, player_id: str, x_takeover_secret: str = Header(...)) -> dict:
    return services.takeover(game_id, player_id, x_takeover_secret)


@device_router.get("/games/{game_id}/me", response_model=MyPlayerState)
def my_state(game_id: str, x_device_token: str = Header(...)) -> dict:
    return services.get_my_state(game_id, x_device_token)


@device_router.post(
    "/games/{game_id}/commands", response_model=CommandAcceptedResponse, status_code=201
)
def submit_command(
    game_id: str, body: CommandRequest, x_device_token: str = Header(...)
) -> dict:
    return services.submit_command(game_id, x_device_token, body.direction)


# ---------------------------------------------------------------------------
# 公开只读接口：只暴露已公告棋盘，不泄露任何提交进度
# ---------------------------------------------------------------------------
@public_router.get("/games/{game_id}", response_model=PublicGameState)
def public_state(game_id: str) -> dict:
    return services.get_public_state(game_id)
