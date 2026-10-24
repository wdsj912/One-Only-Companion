"""Priority model pool failover — mock-level tests, no real network."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.routers.settings import router as settings_router
from app.services import llm_service, settings_service
from app.services.llm_service import LLMError

MSG = [{"role": "user", "content": "hi"}]
SECRET_KEY = "sk-failovertest123456789"


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


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_service, "_FAILOVER_SLEEP_SECONDS", 0)


def test_is_retryable_classification():
    assert llm_service.is_retryable(LLMError("rate", status_code=429)) is True
    assert llm_service.is_retryable(LLMError("server", status_code=503)) is True
    assert llm_service.is_retryable(LLMError("bad request", status_code=400)) is False
    assert llm_service.is_retryable(LLMError("unauthorized", status_code=401)) is False
    assert llm_service.is_retryable(LLMError("forbidden", status_code=403)) is False
    assert llm_service.is_retryable(LLMError("missing", status_code=404)) is False
    assert llm_service.is_retryable(LLMError("no status")) is False
    assert llm_service.is_retryable(httpx.ConnectError("conn")) is True
    assert llm_service.is_retryable(httpx.TimeoutException("timeout")) is True
    assert llm_service.is_retryable(httpx.ReadError("read")) is True


async def test_primary_429_falls_back_to_second(db_session, monkeypatch, no_sleep, caplog):
    calls: list[str] = []

    async def _fake(messages, *, model=None, **kwargs):
        calls.append(model)
        if model == "primary":
            raise LLMError("LLM 429: rate limited", status_code=429)
        return f"reply-from-{model}"

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    with caplog.at_level("WARNING", logger="app.services.llm_service"):
        result = await llm_service.chat_completion_with_fallback(MSG, ["primary", "backup"])

    assert result == "reply-from-backup"
    assert calls == ["primary", "backup"]
    failover_lines = [r for r in caplog.records if "model_failover" in r.getMessage()]
    assert len(failover_lines) == 1
    assert "model_failover from=primary to=backup" in failover_lines[0].getMessage()


async def test_401_raises_immediately_second_not_called(db_session, monkeypatch, no_sleep):
    calls: list[str] = []

    async def _fake(messages, *, model=None, **kwargs):
        calls.append(model)
        raise LLMError("LLM 401: unauthorized", status_code=401)

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    with pytest.raises(LLMError) as excinfo:
        await llm_service.chat_completion_with_fallback(MSG, ["primary", "backup"])

    assert "401" in str(excinfo.value)
    assert calls == ["primary"]


async def test_empty_200_falls_back_to_next_model(db_session, monkeypatch, no_sleep):
    """Some upstreams return HTTP 200 with empty content under load — the pool must move on."""
    calls: list[str] = []

    async def _fake(messages, *, model=None, **kwargs):
        calls.append(model)
        if model == "quiet":
            return ""
        return f"reply-from-{model}"

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    result = await llm_service.chat_completion_with_fallback(MSG, ["quiet", "backup"])
    assert result == "reply-from-backup"
    assert calls == ["quiet", "backup"]


async def test_all_models_429_raises_last_error(db_session, monkeypatch, no_sleep):
    calls: list[str] = []

    async def _fake(messages, *, model=None, **kwargs):
        calls.append(model)
        raise LLMError(f"LLM 429 from {model}", status_code=429)

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    with pytest.raises(LLMError) as excinfo:
        await llm_service.chat_completion_with_fallback(MSG, ["m1", "m2", "m3"])

    assert calls == ["m1", "m2", "m3"]
    assert "m3" in str(excinfo.value), "the LAST error must be raised, not the first"


async def test_raised_error_carries_attempts_for_callers(db_session, monkeypatch, no_sleep):
    """Regression: the test endpoint must read attempts even when the call raises."""

    async def _fake(messages, *, model=None, **kwargs):
        raise LLMError("LLM 400: bad model", status_code=400)

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    with pytest.raises(LLMError) as excinfo:
        await llm_service.chat_completion_with_fallback_detail(MSG, ["only-model"])

    tried = getattr(excinfo.value, "attempts", None)
    assert tried == [{"model": "only-model", "error": "LLM 400: bad model"}]


async def test_test_endpoint_failure_still_reports_attempts(api_client, db_session, monkeypatch, no_sleep):
    await settings_service.set_llm_config(db_session, api_key=SECRET_KEY, model="bad-model")
    await db_session.commit()
    settings_service.invalidate_cache()

    async def _fake(messages, *, model=None, **kwargs):
        raise LLMError("LLM 400: 模型不存在", status_code=400)

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    resp = api_client.post("/api/settings/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["model_used"] is None
    assert [a["model"] for a in body["attempts"]] == ["bad-model"]
    assert SECRET_KEY not in resp.text


async def test_test_endpoint_reports_model_used_and_attempts(api_client, db_session, monkeypatch, no_sleep):
    await settings_service.set_llm_config(db_session, api_key=SECRET_KEY, model="primary-x")
    await settings_service.set_model_priority_list(db_session, ["backup-y"])
    await db_session.commit()
    settings_service.invalidate_cache()

    async def _fake(messages, *, model=None, **kwargs):
        if model == "primary-x":
            raise LLMError("LLM 429: rate limited", status_code=429)
        return "pong"

    monkeypatch.setattr(llm_service, "chat_completion", _fake)

    resp = api_client.post("/api/settings/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model_used"] == "backup-y"
    assert [a["model"] for a in body["attempts"]] == ["primary-x"]
    assert SECRET_KEY not in resp.text
