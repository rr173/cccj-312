"""pytest 夹具：每个用例一个独立的临时 SQLite 库。"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    tmpdir = tempfile.mkdtemp(prefix="race-test-")
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("RACE_DB_PATH", db_path)
    monkeypatch.setenv("RACE_REFEREE_TOKEN", "test-referee-token")
    # 延迟导入，确保配置从环境变量读取
    from race_server.main import app

    with TestClient(app) as c:
        c.headers_ref = {"X-Referee-Token": "test-referee-token"}  # type: ignore[attr-defined]
        yield c


def make_game(
    client: TestClient,
    players: list[str] | None = None,
    *,
    width: int = 5,
    height: int = 5,
    blocked: list[list[int]] | None = None,
    finish: list[list[int]] | None = None,
) -> dict:
    players = players or ["alice", "bob", "carol"]
    finish = finish or [[4, 0]]
    resp = client.post(
        "/referee/games",
        headers=client.headers_ref,  # type: ignore[attr-defined]
        json={
            "name": "t",
            "width": width,
            "height": height,
            "blocked": blocked or [],
            "finish": finish,
            "turn_duration_seconds": 60,
            "players": players,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def start(client: TestClient, game_id: str) -> dict:
    resp = client.post(f"/referee/games/{game_id}/start", headers=client.headers_ref)  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    return resp.json()


def close(client: TestClient, game_id: str) -> dict:
    resp = client.post(f"/referee/games/{game_id}/close", headers=client.headers_ref)  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    return resp.json()


def device_headers(token: str) -> dict[str, str]:
    return {"X-Device-Token": token}


def takeover_headers(secret: str) -> dict[str, str]:
    return {"X-Takeover-Secret": secret}


def submit(client: TestClient, game_id: str, token: str, direction: str):
    return client.post(
        f"/games/{game_id}/commands",
        headers=device_headers(token),
        json={"direction": direction},
    )
