"""In-app LLM settings: service fallback, masking, and API leak tests."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_session
from app.routers.settings import router as settings_router
from app.services import settings_service

FAKE_KEY = "sk-test-1234567890abcdefghijklmnop"


@pytest.fixture
async def db_session():
    settings_service.invalidate_cache()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session = factory()
    yield session
    await session.close()
    await engine.dispose()
    settings_service.invalidate_cache()


@pytest.fixture
async def api_client(db_session):
    app = FastAPI()
    app.include_router(settings_router)

    async def _override_session():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as client:
        yield client


async def test_set_then_get_returns_masked_key(db_session):
    await settings_service.set_llm_config(
        db_session,
        base_url="https://example.test/v1",
        api_key=FAKE_KEY,
        model="test-model",
    )
    await db_session.commit()

    cfg = await settings_service.get_llm_config(db_session)
    masked = settings_service.mask_key(cfg["api_key"])

    assert cfg["api_key"] == FAKE_KEY
    assert masked == FAKE_KEY[:7] + "..." + FAKE_KEY[-4:]
    assert masked != FAKE_KEY
    assert bool(cfg["api_key"]) is True


async def test_api_get_reports_configured(api_client):
    resp = api_client.post(
        "/api/settings",
        json={
            "base_url": "https://example.test/v1",
            "api_key": FAKE_KEY,
            "model": "test-model",
        },
    )
    assert resp.status_code == 200

    body = api_client.get("/api/settings").json()
    assert body["configured"] is True
    assert body["base_url"] == "https://example.test/v1"
    assert body["model"] == "test-model"
    assert body["api_key_masked"] == FAKE_KEY[:7] + "..." + FAKE_KEY[-4:]


async def test_clearing_a_field_falls_back_to_env(db_session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://env.example/v1")

    await settings_service.set_llm_config(db_session, base_url="https://db.example/v1")
    await db_session.commit()
    cfg = await settings_service.get_llm_config(db_session)
    assert cfg["base_url"] == "https://db.example/v1"

    settings_service.invalidate_cache()
    await settings_service.set_llm_config(db_session, base_url="")
    await db_session.commit()
    cfg = await settings_service.get_llm_config(db_session)
    assert cfg["base_url"] == "https://env.example/v1"


def test_mask_key_never_returns_full_key():
    for key in ("", "sk-abc", FAKE_KEY):
        masked = settings_service.mask_key(key)
        if key:
            assert masked != key
            assert key not in masked
        else:
            assert masked == ""


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.test/v1",
        "//example.test/v1",
        "https://user:pass@example.test/v1",
        "javascript:alert(1)",
        "https://example.test/v1\nX-Injected: 1",
    ],
)
async def test_post_rejects_unsafe_base_url(api_client, bad_url):
    """A user-supplied base_url decides where the stored API key is sent."""
    resp = api_client.post("/api/settings", json={"base_url": bad_url})
    assert resp.status_code == 400
    assert "base_url" in resp.json()["detail"]


async def test_post_rejects_private_and_metadata_addresses(api_client):
    """Saving a private endpoint would send the stored key to internal services."""
    for bad in ("http://127.0.0.1:8000/v1", "http://10.0.0.5/v1", "http://169.254.169.254/latest"):
        resp = api_client.post("/api/settings", json={"base_url": bad})
        assert resp.status_code == 400
        assert "base_url" in resp.json()["detail"]


async def test_post_rejects_oversized_fields(api_client):
    assert api_client.post("/api/settings", json={"base_url": "https://example.test/" + "a" * 500}).status_code == 400
    assert api_client.post("/api/settings", json={"api_key": "k" * 513}).status_code == 400
    assert api_client.post("/api/settings", json={"model": "m" * 129}).status_code == 400
    assert api_client.post("/api/settings", json={"model_priority_list": ["m" * 101]}).status_code == 400


async def test_blank_base_url_clears_override(api_client):
    """An empty base_url must still be accepted: it means "fall back to .env"."""
    assert api_client.post("/api/settings", json={"base_url": "https://example.test/v1"}).status_code == 200
    assert api_client.get("/api/settings").json()["base_url"] == "https://example.test/v1"

    assert api_client.post("/api/settings", json={"base_url": ""}).status_code == 200
    assert api_client.get("/api/settings").json()["base_url"] == settings.LLM_BASE_URL


def test_mask_key_hides_short_keys_entirely():
    """A 12-char key would leak 11 of 12 chars with a first7/last4 mask."""
    assert settings_service.mask_key("sk-123456789") == "***"
    assert settings_service.mask_key("a" * 19) == "***"
    assert settings_service.mask_key("a" * 20) == "aaaaaaa" + "..." + "aaaa"


async def test_db_error_returns_clean_503_not_raw_500(api_client, monkeypatch):
    """A locked DB must never surface as '服务器内部错误（StatementError）'."""
    from sqlalchemy.exc import StatementError

    async def _boom(*args, **kwargs):
        raise StatementError("save failed", None, None, Exception("database is locked"))

    monkeypatch.setattr(settings_service, "set_llm_config", _boom)
    resp = api_client.post(
        "/api/settings",
        json={"base_url": "https://example.test/v1", "api_key": FAKE_KEY},
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "2"
    assert "StatementError" not in resp.text
    assert FAKE_KEY not in resp.text


async def test_concurrent_saves_never_hit_unique_constraint(tmp_path):
    """保存按钮连点/双请求时，两个请求同时 SELECT 再各插一行，会撞
    UNIQUE(app_settings.key)。改成一条 upsert 语句后必须全部成功。"""
    import asyncio

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def save():
        async with factory() as s:
            await settings_service.set_llm_config(
                s, base_url="https://example.test/v1", api_key=FAKE_KEY, model="race-model"
            )
            await s.commit()

    await asyncio.gather(*[save() for _ in range(8)])

    async with factory() as s:
        cfg = await settings_service.get_llm_config(s)
    assert cfg["base_url"] == "https://example.test/v1"
    assert cfg["model"] == "race-model"
    await engine.dispose()


async def test_transient_lock_is_retried_then_succeeds(api_client, monkeypatch):
    """手机端定时任务偶尔抢着写锁 —— 重试一次就该存上，而不是让用户看到失败。"""
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}
    real = settings_service.set_llm_config

    async def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("save failed", None, Exception("database is locked"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(settings_service, "set_llm_config", _flaky)
    resp = api_client.post(
        "/api/settings",
        json={"base_url": "https://example.test/v1", "api_key": FAKE_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls["n"] == 2


async def test_post_response_never_contains_raw_key(api_client):
    resp = api_client.post(
        "/api/settings",
        json={"base_url": "https://example.test/v1", "api_key": FAKE_KEY},
    )
    assert resp.status_code == 200
    assert FAKE_KEY not in resp.text

    body = resp.json()
    assert body["ok"] is True
    assert body["api_key_masked"] != FAKE_KEY

    follow_up = api_client.get("/api/settings")
    assert FAKE_KEY not in follow_up.text
    assert follow_up.json()["configured"] is True
