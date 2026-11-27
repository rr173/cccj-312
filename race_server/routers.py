"""HTTP 路由层：只做鉴权、参数绑定与错误映射。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from . import config, services, stakes
from .models import (
    AccountCreated,
    AccountMe,
    AccountSummary,
    Announcement,
    CancelResult,
    CloseTurnRequest,
    CommandAcceptedResponse,
    CommandRequest,
    CreateAccountRequest,
    CreateGameRequest,
    CreateGameResponse,
    CreateStakesGameRequest,
    EnrollmentResult,
    GameSummary,
    MyPlayerState,
    PublicGameState,
    RefereeTurnDetail,
    StakeActionRequest,
    StakesGameCreated,
    StakesState,
    TakeoverResponse,
)
from .services import ServiceError

referee_router = APIRouter(prefix="/referee/games", tags=["referee"])
referee_meta_router = APIRouter(prefix="/referee", tags=["referee"])
account_router = APIRouter(tags=["account"])
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


@referee_router.post(
    "/{game_id}/cancel", response_model=CancelResult,
    dependencies=[Depends(require_referee)],
)
def cancel(game_id: str) -> dict:
    """取消整场积分赛（仅最终完赛前）：仍占用的报名积分全部退回。"""
    return stakes.cancel_game(game_id)


# ---------------------------------------------------------------------------
# 裁定员：账号与积分赛创建
# ---------------------------------------------------------------------------
@referee_meta_router.post(
    "/accounts", response_model=AccountCreated, status_code=201,
    dependencies=[Depends(require_referee)],
)
def create_account(body: CreateAccountRequest) -> dict:
    return stakes.create_account(body)


@referee_meta_router.get(
    "/accounts", response_model=list[AccountSummary],
    dependencies=[Depends(require_referee)],
)
def accounts_list() -> list[dict]:
    return stakes.list_accounts()


@referee_meta_router.post(
    "/stakes-games", response_model=StakesGameCreated, status_code=201,
    dependencies=[Depends(require_referee)],
)
def create_stakes_game(body: CreateStakesGameRequest) -> dict:
    return stakes.create_stakes_game(body)


# ---------------------------------------------------------------------------
# 选手账号接口（请求头 X-Account-Secret）
# ---------------------------------------------------------------------------
@account_router.get("/accounts/me", response_model=AccountMe)
def account_me(x_account_secret: str = Header(...)) -> dict:
    return stakes.get_account_me(x_account_secret)


@account_router.post("/games/{game_id}/stakes/enroll", response_model=EnrollmentResult)
def enroll(
    game_id: str, body: StakeActionRequest, x_account_secret: str = Header(...)
) -> dict:
    return stakes.enroll(game_id, x_account_secret, body.request_id)


@account_router.post("/games/{game_id}/stakes/withdraw", response_model=EnrollmentResult)
def withdraw(
    game_id: str, body: StakeActionRequest, x_account_secret: str = Header(...)
) -> dict:
    return stakes.withdraw(game_id, x_account_secret, body.request_id)


# ---------------------------------------------------------------------------
# 积分赛公开查询：名单、报名进度、终局与分账结果
# ---------------------------------------------------------------------------
@public_router.get("/games/{game_id}/stakes", response_model=StakesState)
def stakes_state(game_id: str) -> dict:
    return stakes.get_stakes_state(game_id)


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
