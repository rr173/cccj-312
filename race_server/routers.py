"""HTTP 路由层：只做鉴权、参数绑定与错误映射。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from . import config, cups, practice, services, stakes
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
    CreateCupRequest,
    CreateGameRequest,
    CreateGameResponse,
    CreatePracticeRoomRequest,
    CreateStakesGameRequest,
    CupCreated,
    CupGameLink,
    CupRematchRequest,
    CupRematchResult,
    CupStartResult,
    CupState,
    CupSummary,
    EnrollmentResult,
    GameSummary,
    MyPlayerState,
    PracticeActionRequest,
    PracticeConfirmResult,
    PracticeDeclineResult,
    PracticeEndResult,
    PracticeGameView,
    PracticeInvitationView,
    PracticeMeView,
    PracticeMyRegistration,
    PracticePairResult,
    PracticeRegistrationResult,
    PracticeRoomSummary,
    PracticeRoomView,
    PublicGameState,
    RefereeTurnDetail,
    StakeActionRequest,
    StakesGameCreated,
    StakesState,
    StartCupMatchRequest,
    TakeoverResponse,
)
from .services import ServiceError

referee_router = APIRouter(prefix="/referee/games", tags=["referee"])
referee_meta_router = APIRouter(prefix="/referee", tags=["referee"])
referee_cup_router = APIRouter(prefix="/referee/cups", tags=["referee"])
referee_practice_router = APIRouter(prefix="/referee/practice-rooms", tags=["referee"])
account_router = APIRouter(tags=["account"])
device_router = APIRouter(tags=["device"])
public_router = APIRouter(tags=["public"])
cup_public_router = APIRouter(prefix="/cups", tags=["cup"])
practice_router = APIRouter(tags=["practice"])
practice_public_router = APIRouter(prefix="/practice-rooms", tags=["practice"])


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
# 裁定员：八人双败杯
# ---------------------------------------------------------------------------
@referee_meta_router.post(
    "/cups", response_model=CupCreated, status_code=201,
    dependencies=[Depends(require_referee)],
)
def create_cup(body: CreateCupRequest) -> dict:
    return cups.create_cup(body)


@referee_meta_router.get(
    "/cups", response_model=list[CupSummary], dependencies=[Depends(require_referee)]
)
def cups_list() -> list[dict]:
    return cups.list_cups()


@referee_cup_router.post(
    "/{cup_id}/matches/{match_no}/start", response_model=CupStartResult,
    dependencies=[Depends(require_referee)],
)
def start_cup_match(cup_id: str, match_no: int, body: StartCupMatchRequest) -> dict:
    return cups.start_match(cup_id, match_no, body.request_id, body.expected_revision)


@referee_cup_router.post(
    "/{cup_id}/games/{game_id}/rematch", response_model=CupRematchResult,
    dependencies=[Depends(require_referee)],
)
def cup_rematch(cup_id: str, game_id: str, body: CupRematchRequest) -> dict:
    return cups.rematch(cup_id, game_id, body.request_id)


# ---------------------------------------------------------------------------
# 杯赛公开查询
# ---------------------------------------------------------------------------
@cup_public_router.get("/{cup_id}", response_model=CupState)
def cup_state(cup_id: str) -> dict:
    return cups.get_cup_state(cup_id)


@cup_public_router.get("/{cup_id}/startable", response_model=list[int])
def cup_startable(cup_id: str) -> list[int]:
    return cups.get_cup_state(cup_id)["startable_matches"]


@cup_public_router.get("/{cup_id}/champion")
def cup_champion(cup_id: str) -> dict:
    state = cups.get_cup_state(cup_id)
    return {
        "cup_id": cup_id,
        "champion_seed_no": state["champion_seed_no"],
        "champion_account_id": state["champion_account_id"],
        "champion_name": state["champion_name"],
        "champion_withdrawn": state["champion_withdrawn"],
    }


@cup_public_router.get("/{cup_id}/invalidations")
def cup_invalidations(cup_id: str) -> list[dict]:
    return cups.get_cup_state(cup_id)["invalidations"]


@cup_public_router.get("/{cup_id}/games", response_model=list[CupGameLink])
def cup_games(cup_id: str) -> list[dict]:
    """杯赛关联的全部对局（含已因重赛失效的）。"""
    return cups.list_cup_games(cup_id)


# 对局 -> 所属杯赛场次（关联对局查询）；非杯赛对局返回 404 not_cup_game
@cup_public_router.get("/games/{game_id}", response_model=CupGameLink)
def game_cup_link(game_id: str) -> dict:
    body = cups.get_game_cup(game_id)
    return {
        "game_id": game_id,
        "cup_id": body["cup_id"],
        "match_no": body["match_no"],
        "order_index": body["order_index"],
        "slot": body["slot"],
        "status": "completed" if body["winner_account_id"] else "in_progress",
        "valid": body["valid"],
        "home_seed_no": body["home_seed_no"],
        "away_seed_no": body["away_seed_no"],
        "winner_account_id": body["winner_account_id"],
        "announcement_turns": body["announcement_turns"],
        "invalidated_reason": body["invalidated_reason"],
        "invalidated_at": body["invalidated_at"],
    }


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


# ---------------------------------------------------------------------------
# 裁定员：练习赛大厅（练习房与提前结束）
# ---------------------------------------------------------------------------
@referee_practice_router.post(
    "", response_model=PracticeRoomView, status_code=201,
    dependencies=[Depends(require_referee)],
)
def create_practice_room(body: CreatePracticeRoomRequest) -> dict:
    return practice.create_room(body)


@referee_practice_router.get(
    "", response_model=list[PracticeRoomSummary],
    dependencies=[Depends(require_referee)],
)
def referee_practice_rooms() -> list[dict]:
    return practice.list_rooms()


@referee_practice_router.post(
    "/{room_id}/pair", response_model=PracticePairResult,
    dependencies=[Depends(require_referee)],
)
def referee_request_pair(room_id: str, body: PracticeActionRequest) -> dict:
    """裁定员请求某房间配对一次：取当前可配对的最早两位。"""
    return practice.request_pair(room_id, body.request_id)


@referee_meta_router.post(
    "/practice-games/{game_id}/end", response_model=PracticeEndResult,
    dependencies=[Depends(require_referee)],
)
def end_practice_game(game_id: str) -> dict:
    """提前结束练习赛：不算完赛，保留已公开公告，不再接手/提交/裁定。"""
    return practice.end_practice_game(game_id)


# ---------------------------------------------------------------------------
# 选手账号：练习赛大厅（请求头 X-Account-Secret）
# ---------------------------------------------------------------------------
@practice_router.post(
    "/practice-rooms/{room_id}/register", response_model=PracticeRegistrationResult
)
def practice_register(
    room_id: str, body: PracticeActionRequest, x_account_secret: str = Header(...)
) -> dict:
    return practice.register(room_id, x_account_secret, body.request_id)


@practice_router.post(
    "/practice-rooms/{room_id}/withdraw", response_model=PracticeRegistrationResult
)
def practice_withdraw(
    room_id: str, body: PracticeActionRequest, x_account_secret: str = Header(...)
) -> dict:
    return practice.withdraw(room_id, x_account_secret, body.request_id)


@practice_router.post(
    "/practice-rooms/{room_id}/pair", response_model=PracticePairResult
)
def practice_request_pair(
    room_id: str, body: PracticeActionRequest, x_account_secret: str = Header(...)
) -> dict:
    """选手请求某房间配对一次（与裁定员接口同一幂等空间）。"""
    return practice.request_pair(room_id, body.request_id)


@practice_router.post(
    "/practice-invitations/{invitation_id}/confirm",
    response_model=PracticeConfirmResult,
)
def practice_confirm(
    invitation_id: str, x_account_secret: str = Header(...)
) -> dict:
    return practice.confirm_invitation(invitation_id, x_account_secret)


@practice_router.post(
    "/practice-invitations/{invitation_id}/decline",
    response_model=PracticeDeclineResult,
)
def practice_decline(
    invitation_id: str, x_account_secret: str = Header(...)
) -> dict:
    return practice.decline_invitation(invitation_id, x_account_secret)


@practice_router.get(
    "/practice-invitations/{invitation_id}", response_model=PracticeInvitationView
)
def practice_invitation(
    invitation_id: str, x_account_secret: str = Header(...)
) -> dict:
    return practice.get_invitation_view(invitation_id, x_account_secret)


@practice_router.get("/practice/me", response_model=PracticeMeView)
def practice_me(x_account_secret: str = Header(...)) -> dict:
    """本人的大厅总览：各房排队状态、待回复邀请与已配成对局。"""
    return practice.get_my_lobby(x_account_secret)


@practice_router.get(
    "/practice/me/registrations", response_model=list[PracticeMyRegistration]
)
def practice_me_registrations(x_account_secret: str = Header(...)) -> list[dict]:
    return practice.get_my_registrations(x_account_secret)


@practice_router.get(
    "/practice/me/invitations", response_model=list[PracticeInvitationView]
)
def practice_me_invitations(x_account_secret: str = Header(...)) -> list[dict]:
    return practice.get_my_invitations(x_account_secret)


@practice_router.get("/practice/me/games", response_model=list[PracticeGameView])
def practice_me_games(x_account_secret: str = Header(...)) -> list[dict]:
    return practice.get_my_games(x_account_secret)


# ---------------------------------------------------------------------------
# 公开：练习房与练习对局查询
# ---------------------------------------------------------------------------
@practice_public_router.get("", response_model=list[PracticeRoomSummary])
def public_practice_rooms() -> list[dict]:
    return practice.list_rooms()


@practice_public_router.get("/{room_id}", response_model=PracticeRoomView)
def public_practice_room(room_id: str) -> dict:
    return practice.get_room_detail(room_id)


@public_router.get("/practice-games/{game_id}", response_model=PracticeGameView)
def public_practice_game(game_id: str) -> dict:
    """对局所属练习房与配对邀请；非练习赛对局返回 404 practice_not_found。"""
    return practice.get_practice_game_view(game_id)
