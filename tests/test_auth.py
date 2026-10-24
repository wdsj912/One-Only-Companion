"""Access-token auth: every data route is 401 without the token."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import security
from app.database import Base, get_session
from app.routers.auth import router as auth_router
from app.routers.memories import router as memories_router

TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture
async def api_client(monkeypatch):
    # Pin the token: get_auth_token() would otherwise read .env / .auth_token.
    monkeypatch.setattr(security, "_token", TOKEN)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = factory()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(memories_router, dependencies=[Depends(security.require_auth)])

    async def _override_session():
        yield session
        await session.commit()

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as client:
        yield client

    await session.close()
    await engine.dispose()


def test_protected_route_requires_credentials(api_client):
    resp = api_client.get("/api/memories", params={"user_id": 1})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "X-API-Token"


def test_header_token_is_accepted(api_client):
    resp = api_client.get("/api/memories", params={"user_id": 1}, headers={"X-API-Token": TOKEN})
    assert resp.status_code == 200


def test_wrong_token_is_rejected(api_client):
    resp = api_client.get("/api/memories", params={"user_id": 1}, headers={"X-API-Token": "nope"})
    assert resp.status_code == 401
    assert api_client.post("/api/auth/login", json={"token": "nope"}).status_code == 401


def test_login_sets_httponly_cookie_that_authenticates(api_client):
    resp = api_client.post("/api/auth/login", json={"token": TOKEN})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie

    # TestClient keeps the cookie, so no header is needed from here on.
    assert api_client.get("/api/memories", params={"user_id": 1}).status_code == 200


def test_logout_clears_the_cookie(api_client):
    api_client.post("/api/auth/login", json={"token": TOKEN})
    assert api_client.get("/api/memories", params={"user_id": 1}).status_code == 200

    api_client.post("/api/auth/logout")
    assert api_client.get("/api/memories", params={"user_id": 1}).status_code == 401


def test_status_is_public_and_reports_login_state(api_client):
    assert api_client.get("/api/auth/status").json() == {"authenticated": False}

    api_client.post("/api/auth/login", json={"token": TOKEN})
    assert api_client.get("/api/auth/status").json() == {"authenticated": True}


def test_generated_token_is_persisted_and_never_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_token", None)
    monkeypatch.setattr(security.settings, "AUTH_TOKEN", "")
    token_file = tmp_path / ".auth_token"
    monkeypatch.setattr(security, "TOKEN_FILE", token_file)

    first = security.get_auth_token()
    assert len(first) >= 32
    assert token_file.read_text(encoding="utf-8").strip() == first

    # A restart must reuse the same token, not invent a new one.
    monkeypatch.setattr(security, "_token", None)
    assert security.get_auth_token() == first
