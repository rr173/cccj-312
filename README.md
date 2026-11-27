# 多人回合制竞速游戏服务端

Python 3.11 + FastAPI + SQLite（WAL 模式）实现的回合制网格竞速游戏服务端。
无前端、无实时推送；客户端通过轮询 REST 接口获取状态。

## 1. 核心规则

每名玩家在网格上控制一枚棋子，每回合可提交一个指令：

- 向北/南/东/西移动到**相邻可通行格**，或 `wait` 原地等待；
- 截止时间前可以反复**替换**，仅最后一个被接受的版本生效，历史版本全部留痕；
- 未交指令等同于 `wait`。

每回合的裁定**严格以回合开始时的棋盘为准**：

1. 多名玩家要进入同一个目标格 → 这些玩家全部留在原处；
2. 其余移动只有当目标格原本为空，或原占用者**最终也移动成功**时才成功；
3. 互换位置（2 环）与更长的闭环（网格上最短为 4 环）整条允许；
4. 想进入等待者/被堵住者所在格的移动失败。

裁定引擎是纯函数（`race_server/engine.py`），不接触数据库，便于单测。

## 2. 并发与一致性模型

- SQLite 开启 WAL；所有写操作都在 `BEGIN IMMEDIATE` 事务中执行，
  写锁把“接手 / 替换指令 / 关回合”整体串行化，锁冲突时应用层有限重试。
- **设备接手**：校验接手密钥后自增 `device_generation`，旧设备令牌立即失效，
  旧设备再写返回 `409 device_superseded`；此前已接受的指令保留，直到新设备替换。
- **关回合**：裁定、写公告、开下一回合在同一个事务内完成，因此
  “关回合后、新回合公开前”不存在可写窗口——事务未提交时新回合对任何设备不可见。
  每回合的裁定与公告严格只发生一次；裁定员可带 `expected_turn_no` 做幂等保护，
  并发重复关回合时多余请求得到 `409 turn_already_closed`，不会误关下一回合。
- **信息可见性**：公开接口只返回上一回合已公告的棋盘与地形，
  不包含任何未公开指令或提交进度；指令内容只有裁定员接口和本设备 `/me` 可见。
- 重连后调用 `GET /games/{id}/me` 即可确认本回合当前生效指令以及是否仍可替换。

## 3. 启动

```bash
pip install -r requirements.txt
export RACE_REFEREE_TOKEN=change-me       # 不设置则进程启动时随机生成
export RACE_DB_PATH=/var/lib/race/race.db  # 默认 ./race_server.db
uvicorn race_server.main:app --host 0.0.0.0 --port 8000
```

交互式文档：启动后访问 `/docs`。

## 4. 接口一览

### 裁定员（请求头 `X-Referee-Token`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/referee/games` | 建局，返回每名玩家的接手密钥与首个设备令牌 |
| GET  | `/referee/games` | 列出所有局 |
| POST | `/referee/games/{game_id}/start` | 开局，玩家布置在左边界可通行格 |
| GET  | `/referee/games/{game_id}/turn` | 当前回合的已接受指令集合（仅裁定员） |
| POST | `/referee/games/{game_id}/close` | 关回合：裁定并公告，原子开启下一回合；body 可带 `{"expected_turn_no": n}` |

### 玩家设备

| 方法 | 路径 | 请求头 | 说明 |
| --- | --- | --- | --- |
| POST | `/games/{game_id}/players/{player_id}/takeover` | `X-Takeover-Secret` | 接手，返回新设备令牌，旧设备立即失效 |
| GET  | `/games/{game_id}/me` | `X-Device-Token` | 查自身状态与本回合指令是否可改 |
| POST | `/games/{game_id}/commands` | `X-Device-Token` | 提交/替换本回合指令 `{"direction": "n\|s\|e\|w\|wait"}` |

### 公开（无需鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/games/{game_id}` | 地形、玩家与**已公告**棋盘、当前回合元数据 |
| GET | `/games/{game_id}/announcements/{turn_no}` | 指定回合的公告 |
| GET | `/health` | 健康检查 |

错误响应统一为 `{"error": "...", "detail": "..."}`。

## 5. 典型流程

```bash
TOK=change-me
G=$(curl -s -X POST localhost:8000/referee/games -H "X-Referee-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"width":8,"height":3,"finish":[[7,0]],"players":["alice","bob"]}')
GID=$(python3 -c "import json,sys;print(json.load(sys.stdin)['game_id'])" <<<"$G")
curl -s -X POST localhost:8000/referee/games/$GID/start -H "X-Referee-Token: $TOK"

DEV=$(python3 -c "import json;print(json.loads('''$G''')['players'][0]['device_token'])")
curl -s -X POST localhost:8000/games/$GID/commands \
  -H "X-Device-Token: $DEV" -H 'Content-Type: application/json' -d '{"direction":"e"}'

curl -s -X POST localhost:8000/referee/games/$GID/close \
  -H "X-Referee-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"expected_turn_no":1}'
```

## 6. 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_engine.py`：裁定纯函数（冲突、跟进、堵截、互换、四环、缺指令）；
- `tests/test_api.py`：HTTP 生命周期、鉴权、信息可见性、接手、端到端裁定；
- `tests/test_concurrency.py`：真实 uvicorn 服务器上的并发接手/提交/关回合。
