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
# 八人双败杯：请求
# ---------------------------------------------------------------------------
class CreateCupRequest(BaseModel):
    name: str = Field(default="cup", max_length=80)
    width: int = Field(default=10, ge=1, le=64)
    height: int = Field(default=10, ge=1, le=64)
    blocked: list[list[int]] = Field(default_factory=list)
    # 所有场次共用的终点格
    finish: list[list[int]] = Field(default_factory=list)
    turn_duration_seconds: int = Field(default=60, ge=1, le=86400)
    # 八个不同账号的账号名，顺序即首轮签位 1..8
    seeds: list[str] = Field(min_length=8, max_length=8)


class StartCupMatchRequest(BaseModel):
    # 请求标识：开赛按此标识幂等重试；旧请求重放只会返回原对局
    request_id: str = Field(min_length=1, max_length=128)
    # 可选的签表版本保护：与当前 revision 不一致时拒绝，避免迟到请求误开新对局
    expected_revision: int | None = None


class CupRematchRequest(BaseModel):
    # 请求标识：重赛按此标识幂等重试
    request_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# 练习赛大厅：请求
# ---------------------------------------------------------------------------
class CreatePracticeRoomRequest(BaseModel):
    name: str = Field(default="practice-room", max_length=80)
    width: int = Field(default=10, ge=1, le=64)
    height: int = Field(default=10, ge=1, le=64)
    blocked: list[list[int]] = Field(default_factory=list)
    finish: list[list[int]] = Field(default_factory=list)
    turn_duration_seconds: int = Field(default=60, ge=1, le=86400)
    # 配对确认时限：邀请发出后双方须在此秒数内确认，否则邀请过期
    confirm_timeout_seconds: int = Field(default=60, ge=1, le=86400)


class PracticeActionRequest(BaseModel):
    # 请求标识：报名/撤销/配对按此标识幂等重试
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
    # 练习赛概要；非练习赛对局为 None
    practice: "PracticeSummary | None" = None


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




# ---------------------------------------------------------------------------
# 八人双败杯：响应
# ---------------------------------------------------------------------------
class CupSeed(BaseModel):
    seed_no: int
    account_id: str
    name: str


class CupCreated(BaseModel):
    cup_id: str
    status: str
    revision: int
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    turn_duration_seconds: int
    seeds: list[CupSeed]
    # 开赛前公布的首轮签表（完整签表可由 GET /cups/{cup_id} 查看）
    first_round: list["CupMatchView"]


class CupSummary(BaseModel):
    cup_id: str
    name: str
    status: str
    revision: int
    champion_account_id: str | None = None


class CupGameLink(BaseModel):
    game_id: str
    cup_id: str
    match_no: int
    order_index: int
    slot: str  # regular | decider
    status: str  # in_progress | completed
    valid: bool
    home_seed_no: int | None = None
    away_seed_no: int | None = None
    winner_account_id: str | None = None
    # 失效对局保留已公开的公告，但不再可接手/提交/裁定
    announcement_turns: int = 0
    # 失效原因（valid=false 时给出）
    invalidated_reason: str | None = None
    invalidated_at: str | None = None


class CupSlotView(BaseModel):
    # 该侧资格的来源签位集合（通常 1 人；总决赛为 2 人）
    candidate_seeds: list[int]
    # 当前已确定的签位；None 表示尚未确定
    seed_no: int | None = None
    account_id: str | None = None
    name: str | None = None


class CupMatchView(BaseModel):
    match_no: int
    bracket: str  # winners | losers | final | decider
    round_no: int
    label: str
    # 晋级资格是否已确定（两侧都确定才能开赛）
    eligible: bool
    # 是否可开赛（有确定资格、尚无有效对局）
    startable: bool
    # 当前有效对局（进行中或已完赛）；None 表示尚无有效对局
    active_game_id: str | None = None
    # 决定该场次结果的已完赛有效对局
    current_game_id: str | None = None
    status: str  # pending | awaiting_players | in_progress | finished
    home: CupSlotView
    away: CupSlotView
    winner_seed_no: int | None = None
    loser_seed_no: int | None = None
    # 该场次关联的全部对局（含已失效的）
    games: list[CupGameLink] = Field(default_factory=list)


class CupInvalidation(BaseModel):
    invalidation_id: str
    rematch_request_id: str
    invalidated_game_id: str
    match_no: int
    scope_slot: str
    reason: str
    turn_no: int | None
    invalidated_at: str
    # 因该局失效而被连带失效的后续对局
    propagated_game_ids: list[str] = Field(default_factory=list)
    champion_withdrawn: bool = False


class CupStartResult(BaseModel):
    cup_id: str
    match_no: int
    game_id: str
    revision: int
    request_id: str
    idempotent_replay: bool
    # 重放旧请求时该对局可能已因重赛失效；不改变历史结果，但明确提示
    game_valid: bool


class CupRematchResult(BaseModel):
    cup_id: str
    invalidated_game_id: str
    match_no: int
    revision: int
    request_id: str
    idempotent_replay: bool
    invalidated_games: list[str]
    champion_withdrawn: bool


class CupStanding(BaseModel):
    seed_no: int
    account_id: str
    name: str
    status: str  # winner | loser | champion | runner_up | eliminated


class CupState(BaseModel):
    cup_id: str
    name: str
    status: str  # setup | active | finished
    revision: int
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    turn_duration_seconds: int
    seeds: list[CupSeed]
    matches: list[CupMatchView]
    standings: list[CupStanding]
    # 当前可开赛的场次编号
    startable_matches: list[int]
    champion_seed_no: int | None = None
    champion_account_id: str | None = None
    champion_name: str | None = None
    # 冠军是否曾被撤回（撤回后若重赛再度产生冠军，会重新给出）
    champion_withdrawn: bool = False
    invalidations: list[CupInvalidation]
    # 请求历史（按 request_id 幂等）
    request_log: list[dict]


# ---------------------------------------------------------------------------
# 练习赛大厅：响应
# ---------------------------------------------------------------------------
class PracticeRoomSummary(BaseModel):
    room_id: str
    name: str
    width: int
    height: int
    blocked: list[list[int]]
    finish: list[list[int]]
    turn_duration_seconds: int
    confirm_timeout_seconds: int
    # 各状态报名数与进行中练习赛场数
    queued: int
    paused: int
    invited: int
    active_games: int
    created_at: str


class PracticeQueueEntry(BaseModel):
    account_id: str
    name: str
    seq: int
    status: str  # queued | paused | invited
    since: str


class PracticeGamePlayer(BaseModel):
    account_id: str
    name: str
    player_id: str


class PracticeGameView(BaseModel):
    game_id: str
    room_id: str
    room_name: str
    invitation_id: str
    status: str  # active | finished | ended
    players: list[PracticeGamePlayer]
    # 仅按账号视角查询时给出本人席位
    my_player_id: str | None = None
    created_at: str
    ended_at: str | None


class PracticeRoomView(PracticeRoomSummary):
    # 进行中的报名队列（queued/paused/invited），按入队顺序
    queue: list[PracticeQueueEntry]
    # 本房配出的全部练习对局
    games: list[PracticeGameView]


class PracticeRegistrationResult(BaseModel):
    room_id: str
    account_id: str
    status: str
    epoch: int
    seq: int
    request_id: str
    # 按请求标识重试时为 true，不消耗新的排队名额
    idempotent_replay: bool


class PracticeInvitationSeat(BaseModel):
    account_id: str
    name: str
    response: str  # pending | confirmed | declined
    responded_at: str | None
    # 开局后该席位的玩家 ID（未开局为 None）
    player_id: str | None


class PracticeInvitationView(BaseModel):
    invitation_id: str
    room_id: str
    room_name: str
    status: str  # pending | started | declined | expired
    deadline: str
    game_id: str | None
    seats: list[PracticeInvitationSeat]
    # 按账号视角查询时给出本人应答
    my_response: str | None = None
    created_at: str


class PracticePairResult(BaseModel):
    room_id: str
    paired: bool
    invitation_id: str | None
    invitation: PracticeInvitationView | None
    request_id: str
    idempotent_replay: bool


class PracticeConfirmResult(BaseModel):
    invitation_id: str
    room_id: str
    status: str
    my_response: str
    all_confirmed: bool
    # 第二人确认时开出对局：started=true 并给出 game_id
    started: bool
    game_id: str | None
    idempotent_replay: bool
    invitation: PracticeInvitationView


class PracticeDeclineResult(BaseModel):
    invitation_id: str
    room_id: str
    status: str
    my_response: str
    idempotent_replay: bool
    invitation: PracticeInvitationView


class PracticeMyRegistration(BaseModel):
    room_id: str
    room_name: str
    status: str
    epoch: int
    seq: int
    # 在可配对队列中的位置（1 起）；非 queued 状态为 None
    position: int | None
    request_id: str
    created_at: str
    updated_at: str


class PracticeMeView(BaseModel):
    account_id: str
    name: str
    # 本人在各房间的报名（含已终结的历史状态）
    registrations: list[PracticeMyRegistration]
    # 本人待处理的配对邀请
    invitations: list[PracticeInvitationView]
    # 本人已配成的练习对局
    games: list[PracticeGameView]


class PracticeEndResult(BaseModel):
    game_id: str
    room_id: str
    status: str
    ended_at: str
    # 随结束放回大厅排队的账号
    released_accounts: list[str]


class PracticeSummary(BaseModel):
    room_id: str
    invitation_id: str
    status: str


# PracticeSummary 定义在文件后部，显式重建以解析前向引用
PublicGameState.model_rebuild()
