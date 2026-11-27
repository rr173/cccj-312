"""令牌生成与哈希工具。

原始令牌只在创建时返回一次，数据库中仅保存 HMAC-SHA256 哈希；
数据库实例级 pepper 存于 meta 表，随库文件一起迁移。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_secret(pepper: str, raw: str) -> str:
    return hmac.new(pepper.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_secret(pepper: str, raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(pepper, raw), stored_hash)
