"""运行配置。

所有配置都从环境变量读取，便于测试时按用例隔离数据库。
"""
from __future__ import annotations

import hmac
import os
import secrets

# 进程内兜底的裁定员令牌：未通过环境变量提供时随机生成，重启即失效。
_FALLBACK_REFEREE_TOKEN = "dev-" + secrets.token_urlsafe(18)


def database_path() -> str:
    """SQLite 数据库文件路径，每次调用都读取环境变量。"""
    return os.environ.get("RACE_DB_PATH", "race_server.db")


def referee_token() -> str:
    """裁定员令牌（X-Referee-Token）。"""
    return os.environ.get("RACE_REFEREE_TOKEN", _FALLBACK_REFEREE_TOKEN)


def referee_token_matches(token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, referee_token())
