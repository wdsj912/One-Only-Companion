"""Background Memory Agent (Librarian) tests — the LLM is always mocked."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models as m
from app.database import Base
from app.services import memory_agent
from app.services.memory_agent import (
    _extract_json_array,
    process_memory,
    run_memory_agent,
)


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield f
    await engine.dispose()


@pytest.fixture
async def session(factory):
    s = factory()
    yield s
    await s.close()


def _mock_llm(monkeypatch, payload, captured=None):
    async def _fake(messages, **kwargs):
        if captured is not None:
            captured.append(messages)
        return payload

    monkeypatch.setattr(memory_agent, "chat_completion", _fake)


def _no_log(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_agent, "LOG_PATH", tmp_path / "_memory_agent.log")


# --- JSON extraction ----------------------------------------------------------

def test_extract_plain_array():
    assert _extract_json_array('[{"action": "ADD"}]') == [{"action": "ADD"}]


def test_extract_fenced_and_prose():
    raw = 'Sure, here you go:\n```json\n[{"action": "DELETE", "memory_id": 5}]\n```\nHope that helps.'
    assert _extract_json_array(raw) == [{"action": "DELETE", "memory_id": 5}]


def test_mock_llm_memory_reply_is_valid_json():
    """MOCK_LLM 模式下的 Librarian 回复必须能解析。

    以前这里是 `"mock_" + abs(hash(text)) % 10000`（str + int），每次 mock 跑
    记忆提取都会抛 TypeError 被静默吞掉 —— 压力测试看着全绿，实际记忆一条没写。
    """
    from app.services import llm_service

    messages = [
        {"role": "system", "content": memory_agent.MEMORY_AGENT_PROMPT.format(
            existing_memories="[]", recent_messages="user: 我明天要去面试，有点紧张")},
        {"role": "user", "content": "Decide the memory operations now. JSON array only."},
    ]
    ops = _extract_json_array(llm_service._mock_reply(messages))
    assert ops is not None
    assert ops[0]["action"] == "ADD"
    assert ops[0]["fact_key"].startswith("mock.")
    # 内容取自 Recent Chat 里的聊天正文，而不是「Decide the memory operations now」这句指令
    assert "面试" in ops[0]["content"]


def test_normal_chat_is_not_misread_as_a_memory_call():
    """普通聊天不能被当成 Librarian —— 动态 prompt 里的 [Fact] 曾让 mock
    把一段 JSON 当回复发给用户。"""
    from app.services import llm_service
    from app.services.prompt_builder import STATIC_PERSONA

    messages = [
        {"role": "system", "content": STATIC_PERSONA},
        {"role": "system", "content": "# What you remember about the user\n[Fact] The user likes cats"},
        {"role": "user", "content": "在吗"},
    ]
    raw = llm_service._mock_reply(messages)
    assert not raw.strip().startswith("[")


def test_extract_invalid_returns_none():
    assert _extract_json_array("no json here") is None
    assert _extract_json_array('{"action": "ADD"}') is None  # object, not array
    assert _extract_json_array("") is None


# --- ADD ----------------------------------------------------------------------

async def test_add_fact_and_event(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    _mock_llm(monkeypatch, json.dumps([
        {"action": "ADD", "type": "fact", "fact_key": "user.name", "content": "The user's name is Azhe", "emotion": "neutral"},
        {"action": "ADD", "type": "event", "content": "The user got scolded by the boss today", "emotion": "angry"},
        {"action": "ADD", "type": "preference", "content": "The user hates rainy days"},
    ], ensure_ascii=False))

    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    await session.commit()

    rows = list((await session.execute(select(m.Memory))).scalars().all())
    assert len(rows) == 3
    by_layer = {(r.layer, r.fact_key): r for r in rows}
    fact = by_layer[("semantic", "user.name")]
    assert fact.content == "The user's name is Azhe"
    assert fact.emotion is None  # "neutral" is stored as NULL
    event = by_layer[("episodic", None)]
    assert event.emotion == "angry"
    pref = by_layer[("semantic", [r.fact_key for r in rows if r.fact_key and r.fact_key.startswith("preference.")][0])]
    assert "rainy days" in pref.content
    assert results == ["ADD fact user.name", "ADD event", f"ADD fact {pref.fact_key}"]


async def test_add_same_fact_key_upserts_instead_of_duplicating(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    payload = json.dumps([{"action": "ADD", "type": "fact", "fact_key": "user.name", "content": "v1"}])
    _mock_llm(monkeypatch, payload)
    await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    await session.commit()

    _mock_llm(monkeypatch, json.dumps([{"action": "ADD", "type": "fact", "fact_key": "user.name", "content": "v2"}]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    await session.commit()

    rows = list((await session.execute(select(m.Memory))).scalars().all())
    assert len(rows) == 1
    assert rows[0].content == "v2"
    assert results == ["ADD→UPDATE fact user.name"]


async def test_add_rejects_empty_content(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    _mock_llm(monkeypatch, '[{"action": "ADD", "content": "   "}]')
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    assert results == ["ADD skipped: empty content"]
    assert list((await session.execute(select(m.Memory))).scalars().all()) == []


# --- UPDATE / DELETE ------------------------------------------------------------

async def test_update_changes_existing_row_in_place(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    mem = m.Memory(user_id=1, layer="semantic", fact_key="preference.cat", content="豆豆只吃鸡肉罐头", fact_value="豆豆只吃鸡肉罐头")
    session.add(mem)
    await session.flush()
    mid = mem.id

    _mock_llm(monkeypatch, json.dumps([{"action": "UPDATE", "memory_id": mid, "content": "豆豆现在也吃鱼肉罐头"}]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[mem])
    await session.commit()

    rows = list((await session.execute(select(m.Memory))).scalars().all())
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].id == mid
    assert rows[0].content == "豆豆现在也吃鱼肉罐头"
    assert rows[0].fact_value == "豆豆现在也吃鱼肉罐头"
    assert results == [f"UPDATE #{mid}"]


async def test_delete_soft_deletes(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    mem = m.Memory(user_id=1, layer="episodic", content="用户今天感冒了")
    session.add(mem)
    await session.flush()
    mid = mem.id

    _mock_llm(monkeypatch, json.dumps([{"action": "DELETE", "memory_id": mid}]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[mem])
    await session.commit()
    await session.refresh(mem)

    assert mem.deleted_at is not None
    assert results == [f"DELETE #{mid}"]


async def test_ops_cannot_touch_other_users_memories(session, monkeypatch, tmp_path):
    """A hallucinated/forged memory_id must never cross the user boundary."""
    _no_log(monkeypatch, tmp_path)
    foreign = m.Memory(user_id=2, layer="semantic", fact_key="user.name", content="someone else")
    session.add(foreign)
    await session.flush()
    fid = foreign.id

    _mock_llm(monkeypatch, json.dumps([
        {"action": "UPDATE", "memory_id": fid, "content": "hijacked"},
        {"action": "DELETE", "memory_id": fid},
    ]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    await session.commit()
    await session.refresh(foreign)

    assert foreign.content == "someone else"
    assert foreign.deleted_at is None
    assert results == [f"UPDATE #{fid} skipped: unknown id", f"DELETE #{fid} skipped: unknown id"]


async def test_garbage_llm_output_writes_nothing(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    _mock_llm(monkeypatch, "I think the user is nice!")  # no JSON array
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    assert results == ["parse error: no JSON array in LLM output"]
    assert list((await session.execute(select(m.Memory))).scalars().all()) == []


# --- Background task ------------------------------------------------------------

async def test_run_memory_agent_end_to_end(factory, session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    conv = m.Conversation(user_id=1)
    session.add(conv)
    await session.flush()
    session.add_all([
        m.Message(conversation_id=conv.id, role="user", content="我叫阿哲"),
        m.Message(conversation_id=conv.id, role="assistant", content="阿哲，记住啦"),
        # open threads are managed by their own lifecycle — invisible to the Librarian
        m.Memory(user_id=1, layer="episodic", fact_key="open_thread", content="我明天要去面试"),
    ])
    await session.commit()

    captured: list = []
    _mock_llm(monkeypatch, "[{\"action\": \"ADD\", \"type\": \"fact\", \"fact_key\": \"user.name\", \"content\": \"The user's name is Azhe\"}]", captured)

    await run_memory_agent(conv.id, session_factory=factory)

    rows = list((await session.execute(
        select(m.Memory).where(m.Memory.fact_key == "user.name")
    )).scalars().all())
    assert len(rows) == 1 and rows[0].content == "The user's name is Azhe"

    # The prompt must contain the chat log but NOT the open_thread row.
    prompt = captured[0][0]["content"]
    assert "我叫阿哲" in prompt
    assert "open_thread" not in prompt
    assert "我明天要去面试" not in prompt

    # Audit log captured the raw LLM output.
    log_text = (tmp_path / "_memory_agent.log").read_text(encoding="utf-8")
    entry = json.loads(log_text.strip().splitlines()[-1])
    assert entry["conversation_id"] == conv.id
    assert "user.name" in entry["llm_output"]
    assert entry["results"] == ["ADD fact user.name"]


async def test_run_memory_agent_retries_on_429(factory, session, monkeypatch, tmp_path):
    """The agent fires right after the chat call — provider rate limits are
    transient and must be retried, not silently dropped."""
    _no_log(monkeypatch, tmp_path)
    conv = m.Conversation(user_id=1)
    session.add(conv)
    await session.flush()
    session.add(m.Message(conversation_id=conv.id, role="user", content="我叫阿哲"))
    await session.commit()

    calls = {"n": 0}

    async def _flaky(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise memory_agent.LLMError("rate limited", status_code=429)
        return '[{"action": "ADD", "type": "fact", "fact_key": "user.name", "content": "Azhe"}]'

    monkeypatch.setattr(memory_agent, "chat_completion", _flaky)
    monkeypatch.setattr(memory_agent.asyncio, "sleep", _instant)  # no real backoff in tests

    await run_memory_agent(conv.id, session_factory=factory)
    assert calls["n"] == 2
    assert (await session.execute(select(m.Memory).where(m.Memory.fact_key == "user.name"))).scalar_one_or_none() is not None


async def _instant(*args, **kwargs):
    return None


async def test_run_memory_agent_survives_llm_failure(factory, session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    conv = m.Conversation(user_id=1)
    session.add(conv)
    await session.flush()
    session.add(m.Message(conversation_id=conv.id, role="user", content="hello"))
    await session.commit()

    async def _boom(messages, **kwargs):
        from app.services.llm_service import LLMError
        raise LLMError("upstream down")

    monkeypatch.setattr(memory_agent, "chat_completion", _boom)
    await run_memory_agent(conv.id, session_factory=factory)  # must not raise

    entry = json.loads((tmp_path / "_memory_agent.log").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "upstream down" in entry["error"]


# --- fact_key fallback (models sometimes omit memory_id) ----------------------

async def test_update_by_fact_key_updates_in_place(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    mem = m.Memory(user_id=1, layer="semantic", fact_key="preference.pet.diet",
                   content="豆豆只吃鸡肉罐头", fact_value="豆豆只吃鸡肉罐头")
    session.add(mem)
    await session.flush()
    mid = mem.id

    # No memory_id — the model targets by fact_key + type=preference, and the
    # stored row carries the "preference." prefix applied by _apply_add.
    _mock_llm(monkeypatch, json.dumps([
        {"action": "UPDATE", "type": "preference", "fact_key": "pet.diet",
         "content": "豆豆也开始吃鱼肉罐头"}
    ]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[mem])
    await session.commit()

    rows = list((await session.execute(select(m.Memory))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == mid
    assert rows[0].content == "豆豆也开始吃鱼肉罐头"
    assert results == [f"UPDATE #{mid}"]


async def test_update_by_fact_key_missing_target_falls_back_to_add(session, monkeypatch, tmp_path):
    """The exact live failure: turn-1 was rate-limited, so the correction's
    UPDATE had no row to hit — it must become an ADD, not a skip."""
    _no_log(monkeypatch, tmp_path)
    _mock_llm(monkeypatch, json.dumps([
        {"action": "UPDATE", "type": "preference", "fact_key": "pet.dou_dou.diet",
         "content": "Dou Dou is picky but has started eating fish flavor canned food"}
    ]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[])
    await session.commit()

    rows = list((await session.execute(select(m.Memory))).scalars().all())
    assert len(rows) == 1
    assert rows[0].fact_key == "preference.pet.dou_dou.diet"
    assert "fish" in rows[0].content
    assert results == ["ADD fact preference.pet.dou_dou.diet"]


async def test_delete_by_fact_key(session, monkeypatch, tmp_path):
    _no_log(monkeypatch, tmp_path)
    mem = m.Memory(user_id=1, layer="episodic", fact_key="event.cold", content="用户感冒了")
    session.add(mem)
    await session.flush()
    mid = mem.id

    _mock_llm(monkeypatch, json.dumps([{"action": "DELETE", "fact_key": "event.cold"}]))
    results = await process_memory(session, user_id=1, recent_messages=[], existing_memories=[mem])
    await session.commit()
    await session.refresh(mem)

    assert mem.deleted_at is not None
    assert results == [f"DELETE #{mid}"]


async def test_concurrent_runs_on_same_conversation_are_serialized(factory, session, monkeypatch, tmp_path):
    """Two rapid messages spawn two Librarian runs; without the per-conversation
    lock both would read the same pre-write state and could double-save."""
    _no_log(monkeypatch, tmp_path)
    conv = m.Conversation(user_id=1)
    session.add(conv)
    await session.flush()
    session.add(m.Message(conversation_id=conv.id, role="user", content="hi"))
    await session.commit()

    import asyncio as aio
    active = 0
    max_active = 0

    async def _slow(messages, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await aio.sleep(0.05)
        active -= 1
        return "[]"

    monkeypatch.setattr(memory_agent, "chat_completion", _slow)
    await aio.gather(
        run_memory_agent(conv.id, session_factory=factory),
        run_memory_agent(conv.id, session_factory=factory),
    )
    assert max_active == 1  # the second run waited for the first to finish
