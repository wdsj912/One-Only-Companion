"""LLM failures must not roll back the user's turn.

Two guarantees:
  (a) a network error (httpx.ConnectError) returns 200 and keeps the user
      message + extracted semantic fact in the database;
  (b) a missing API key returns 200 with the in-app settings prompt.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models as m
from app.database import Base, get_session
from app.routers import chat as chat_module
from app.routers.chat import LLM_UNAVAILABLE_REPLY, router as chat_router
from app.services import llm_service
from app.services import reflection as reflection_module
from app.services import settings_service

USER_TEXT = "我叫阿哲，今天有点累"


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
async def api_client(db_session, monkeypatch):
    from app.services import memory_agent

    app = FastAPI()
    app.include_router(chat_router)

    async def _override_session():
        yield db_session
        await db_session.commit()

    # The Librarian must never fire in tests — it would open the real DB and
    # call the real LLM. Record spawns instead so tests can assert scheduling.
    spawned: list[int] = []
    monkeypatch.setattr(memory_agent, "spawn", lambda conv_id: spawned.append(conv_id))

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as client:
        yield client, spawned


async def test_llm_connect_error_preserves_user_message_and_schedules_librarian(api_client, db_session, monkeypatch):
    """The chat path stores nothing but messages; memory is the Librarian's job."""
    api_client, spawned = api_client

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(chat_module, "chat_completion_with_fallback", _boom)

    resp = api_client.post("/api/chat/send", json={"user_id": 1, "text": USER_TEXT})

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == LLM_UNAVAILABLE_REPLY
    assert body["llm_unavailable"] is True

    messages = (await db_session.execute(
        select(m.Message).where(m.Message.content == USER_TEXT)
    )).scalars().all()
    assert any(msg.role == "user" for msg in messages), "user message must survive the LLM failure"

    # No synchronous extraction on the hot path — the turn is handed to the
    # background Memory Agent instead (even when the chat model is down).
    assert not (await db_session.execute(select(m.Memory))).scalars().all()
    assert spawned == [body["conversation_id"]]


async def test_missing_api_key_returns_config_prompt_with_200(api_client, db_session, monkeypatch):
    api_client, _ = api_client
    async def _no_key(*args, **kwargs):
        return {"base_url": "https://example.test/v1", "api_key": "", "model": "test-model"}

    monkeypatch.setattr(llm_service, "get_llm_config", _no_key)

    resp = api_client.post("/api/chat/send", json={"user_id": 1, "text": USER_TEXT})

    assert resp.status_code == 200
    assert resp.json()["reply"] == LLM_UNAVAILABLE_REPLY

    messages = (await db_session.execute(
        select(m.Message).where(m.Message.content == USER_TEXT)
    )).scalars().all()
    assert any(msg.role == "user" for msg in messages)


def test_sse_frame_splits_multiline_payload():
    """A delta containing a newline must not truncate the SSE frame."""
    assert chat_module._sse_frame("hello") == "data: hello\n\n"
    assert chat_module._sse_frame("a\nb") == "data: a\ndata: b\n\n"
    assert chat_module._sse_frame("a\n\nb") == "data: a\ndata: \ndata: b\n\n"


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x" * 4001},
        {"text": "   "},
        {"text": ""},
        {"user_id": 0, "text": "hello"},
        {"user_id": 1, "conversation_id": 0, "text": "hello"},
    ],
)
async def test_send_rejects_invalid_payload(api_client, payload):
    api_client, _ = api_client
    resp = api_client.post("/api/chat/send", json=payload)
    assert resp.status_code == 422


async def test_send_accepts_max_length_message(api_client, monkeypatch):
    api_client, spawned = api_client

    async def _reply(*args, **kwargs):
        return "嗯"

    monkeypatch.setattr(chat_module, "chat_completion_with_fallback", _reply)

    resp = api_client.post("/api/chat/send", json={"user_id": 1, "text": "x" * 4000})
    assert resp.status_code == 200
    assert spawned == [resp.json()["conversation_id"]]


async def test_reflection_skips_llm_when_no_api_key(db_session, monkeypatch):
    """FIX 4: llm_available must reflect the real key, not an always-true expression."""
    async def _no_key(*args, **kwargs):
        return {"base_url": "https://example.test/v1", "api_key": "", "model": "test-model"}

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("reflection must not call the LLM without an API key")

    monkeypatch.setattr(chat_module, "get_llm_config", _no_key)
    monkeypatch.setattr(reflection_module, "chat_completion", _must_not_be_called)

    user = m.User(id=1)
    user.persona_state = m.PersonaState()
    conv = m.Conversation(user_id=1)
    db_session.add_all([user, conv])
    await db_session.flush()
    for i in range(4):
        db_session.add(m.Message(conversation_id=conv.id, role="user" if i % 2 == 0 else "assistant", content=f"turn {i}"))
    await db_session.flush()

    await chat_module._memory_consolidation_and_reflection(db_session, user, conv)

    rows = (await db_session.execute(
        select(m.SelfReflection).where(m.SelfReflection.user_id == 1)
    )).scalars().all()
    assert len(rows) == 1
    assert "fallback" in rows[0].content, "no-key reflection must use the deterministic fallback"
