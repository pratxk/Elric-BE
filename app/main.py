"""Elric backend — FastAPI entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.routes import router as api_router
from app.infra.db import init_db
from app.infra.jwt import require_jwt_secret

app = FastAPI(title="Elric — AP Invoice Processing Agent", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    # Fail loudly without a JWT secret — no silent fallback, ever.
    require_jwt_secret()
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "elric-backend", "version": "0.1.0"}


app.include_router(auth_router, prefix="/api")
app.include_router(api_router, prefix="/api")
