"""FastAPI 应用入口。

运行：
    RACE_REFEREE_TOKEN=secret uvicorn race_server.main:app --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_schema
from .routers import (
    account_router,
    cup_public_router,
    device_router,
    practice_public_router,
    practice_router,
    public_router,
    referee_cup_router,
    referee_meta_router,
    referee_practice_router,
    referee_router,
)
from .services import ServiceError


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_schema()
    yield


app = FastAPI(title="多人回合制竞速游戏服务端", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "detail": exc.detail},
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(public_router)
app.include_router(cup_public_router)
app.include_router(practice_public_router)
app.include_router(account_router)
app.include_router(practice_router)
app.include_router(device_router)
app.include_router(referee_router)
app.include_router(referee_meta_router)
app.include_router(referee_cup_router)
app.include_router(referee_practice_router)
