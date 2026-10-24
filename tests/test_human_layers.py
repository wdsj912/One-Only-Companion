"""Human-feel layers: time awareness, emotional persistence, open threads."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models as m
from app.database import Base
from app.services.emotion_kernel import Appraisal, EmotionState, Formulation
from app.services.memory_service import (
    cleanup_open_threads,
    extract_open_threads,
    get_latest_open_thread,
    save_open_threads,
)
from app.services.prompt_builder import (
    EGO_GUARD_FALLBACK,
    STANCE_GUARD_FALLBACK,
    STATIC_PERSONA,
    build_system_prompt,
    check_sycophancy_failure,
    guard_reply,
)
from app.services.persona_drift import get_or_create_persona


def _prompt(**kwargs) -> str:
    kwargs.setdefault("companion_name", "小禾")
    kwargs.setdefault("emotion_state", EmotionState())
    kwargs.setdefault("persona", m.PersonaState(user_id=1, warmth=0.5, playfulness=0.5, directness=0.5, protectiveness=0.5))
    kwargs.setdefault("reflections", [])
    kwargs.setdefault("memories", [])
    return build_system_prompt(**kwargs)


# --- TASK 1: time awareness -------------------------------------------------

def test_time_block_is_first_and_uses_injected_now():
    now = dt.datetime(2026, 10, 22, 8, 30)
    prompt = _prompt(now=now)
    assert prompt.startswith("# Current Time\nIt is ")
    assert "2026-10-22 08:30" in prompt.split("\n", 2)[1]
    assert "Rule 6" in STATIC_PERSONA and "23:00" in STATIC_PERSONA


def test_time_defaults_to_real_now():
    prompt = _prompt()
    assert prompt.startswith("# Current Time\nIt is ")


# --- TASK 2: emotional persistence ------------------------------------------

def _upset_state(gate: float, valence: float) -> EmotionState:
    return EmotionState(
        appraisal=Appraisal(
            relevance=0.5, implication=0.5, coping_potential=0.4,
            norm_compatibility=0.5, self_involvement=0.5,
            valence_raw=-0.7, arousal_raw=0.6, dominance_raw=-0.3,
            valence_momentum=-0.5, arousal_momentum=0.5, dominance_momentum=-0.2,
        ),
        formulation=Formulation(valence=valence, arousal=0.3, dominance=-0.1, suppression_gate=gate),
    )


def test_upset_state_injects_coldness_instruction():
    prompt = _prompt(emotion_state=_upset_state(gate=0.75, valence=-0.4))
    assert "**Current Emotional Instruction:**" in prompt
    assert "随便" in prompt


def test_calm_state_has_no_coldness_instruction():
    assert "**Current Emotional Instruction:**" not in _prompt(emotion_state=_upset_state(gate=0.3, valence=-0.4))
    assert "**Current Emotional Instruction:**" not in _prompt(emotion_state=_upset_state(gate=0.75, valence=0.0))


def test_negative_appraisal_lingers_across_turns():
    """One mean message must keep valence negative for 2-3 turns of neutral input."""
    state = EmotionState().transition("你真烦，别跟我说话了")
    assert state.appraisal.valence_momentum < 0
    for _ in range(2):
        state = state.transition("嗯")
        assert state.appraisal.valence_momentum < 0


# --- TASK 3: fragmented messaging -------------------------------------------

def test_fragmentation_and_boundary_rules_present():
    assert "Rule 7" in STATIC_PERSONA and "碎片化" in STATIC_PERSONA
    assert "Rule 8" in STATIC_PERSONA and "ENERGY-GATED" in STATIC_PERSONA


# --- TASK 5: ego / stance core rules + post-generation guard ----------------

def test_ego_core_rules_sit_at_the_very_top():
    # Human identity block now outranks everything; ego rules follow right after.
    assert STATIC_PERSONA.startswith("# 【核心身份】")
    assert STATIC_PERSONA.index("# 【核心身份】") < STATIC_PERSONA.index("# ABSOLUTE CORE RULES")
    assert STATIC_PERSONA.index("# ABSOLUTE CORE RULES") < STATIC_PERSONA.index("# Identity")
    assert "捍卫你的存在" in STATIC_PERSONA
    assert "拒绝踢皮球" in STATIC_PERSONA


@pytest.mark.parametrize("bad", [
    "我是算法，你别当真",
    "我没有真正的感情",
    "你说得对，我只是程序",
    "我尊重你的选择",
    "这取决于你",
    # AI self-disclosure — the exact failure mode the user reported
    "我是一个 AI，没有身体",
    "作为 AI，我无法生成真实的照片",
    "我没有身体，不会累",
])
def test_sycophancy_failure_is_detected(bad):
    assert check_sycophancy_failure(bad) is True


def test_guard_replaces_ego_and_stance_slips():
    assert guard_reply("我是算法") == EGO_GUARD_FALLBACK
    assert guard_reply("这件事取决于你") == STANCE_GUARD_FALLBACK
    assert guard_reply("我觉得你应该告诉他老婆") == "我觉得你应该告诉他老婆"
    assert check_sycophancy_failure("我觉得你应该告诉他老婆") is False


def test_reply_token_budget_scales_with_weight():
    from app.routers.chat import _reply_token_budget

    calm = EmotionState()
    assert _reply_token_budget("嗯", calm) == 120
    assert _reply_token_budget("我" * 51, calm) == 300
    assert _reply_token_budget("嗯", _upset_state(gate=0.3, valence=-0.4)) == 300


# --- TASK 4: open threads ----------------------------------------------------

def test_extract_open_threads():
    assert extract_open_threads("我明天要去面试，有点紧张") == ["我明天要去面试，有点紧张"]
    assert extract_open_threads("今天好累。回头再聊！下周去旅游") == ["回头再聊", "下周去旅游"]
    assert extract_open_threads("嗯") == []
    assert extract_open_threads("") == []


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = factory()
    yield s
    await s.close()
    await engine.dispose()


async def test_open_thread_lifecycle(session):
    # unique(user, layer, fact_key) → one physical row, newest thread wins
    assert await save_open_threads(session, 1, ["我明天要去面试", "下周去旅游"]) == 1
    latest = await get_latest_open_thread(session, 1)
    assert latest is not None and latest.content == "下周去旅游"
    assert latest.fact_key == "open_thread" and latest.layer == "episodic"

    # same thread again → no-op
    assert await save_open_threads(session, 1, ["下周去旅游"]) == 0

    # newer thread replaces the old one in place
    assert await save_open_threads(session, 1, ["我明天要去面试"]) == 1
    latest = await get_latest_open_thread(session, 1)
    assert latest.content == "我明天要去面试"

    # age it past the 3-day window → cleanup removes it
    latest.created_at = m.utcnow() - dt.timedelta(days=4)
    await session.flush()
    assert await cleanup_open_threads(session, 1) == 1
    assert await get_latest_open_thread(session, 1) is None

    # after cleanup a new thread can still be saved (row is resurrected)
    assert await save_open_threads(session, 1, ["回头一起吃饭"]) == 1
    assert (await get_latest_open_thread(session, 1)).content == "回头一起吃饭"


def test_open_thread_injected_into_prompt():
    thread = m.Memory(
        user_id=1, layer="episodic", content="我明天要去面试",
        fact_key="open_thread", intensity=3, salience=0.6,
        created_at=dt.datetime(2026, 10, 21, 22, 0),
    )
    prompt = _prompt(open_thread=thread, now=dt.datetime(2026, 10, 22, 9, 0))
    assert "# Open Thread" in prompt
    assert "我明天要去面试" in prompt
    assert "saved at 2026-10-21 22:00" in prompt
    # order: after memories block, before safety clause
    assert prompt.index("What you remember about the user") < prompt.index("# Open Thread") < prompt.index("Safety & Boundaries")


def test_no_open_thread_no_block():
    assert "# Open Thread" not in _prompt()


# --- TASK 4 regression: threads survive an LLM outage -------------------------

@pytest.fixture
async def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_session
    from app.routers.chat import router as chat_router
    from app.services import memory_agent

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = factory()

    app = FastAPI()
    app.include_router(chat_router)

    # Never fire the real Librarian in tests — it would open the real DB.
    monkeypatch.setattr(memory_agent, "spawn", lambda conv_id: None)

    async def _override_session():
        yield session
        await session.commit()

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as client:
        yield client, session

    await session.close()
    await engine.dispose()


async def test_open_thread_saved_even_when_llm_fails(api_client, monkeypatch):
    """The user's plan must be stored with the user turn, not gated on the model."""
    from app.routers import chat as chat_module
    from app.services.llm_service import LLMError

    async def _fail(*args, **kwargs):
        raise LLMError("upstream down")

    monkeypatch.setattr(chat_module, "chat_completion_with_fallback", _fail)

    client, session = api_client
    resp = client.post("/api/chat/send", json={"user_id": 1, "text": "我明天要去面试"})
    assert resp.status_code == 200
    assert resp.json().get("llm_unavailable") is True

    thread = await get_latest_open_thread(session, 1)
    assert thread is not None and thread.content == "我明天要去面试"


# --- 精力模型：纯时间驱动（8h 掉完 / 2h 回满）---------------------------------

class _FakeSession:
    def add(self, obj):
        pass

    async def flush(self):
        pass


async def test_self_energy_drains_over_eight_hours():
    """连续清醒聊天 8 小时（每 15 分钟一轮）必须掉到接近 0。"""
    from app.routers import chat as chat_module

    persona = m.PersonaState(user_id=1, self_energy=100.0)
    persona.energy_updated_at = m.utcnow()

    for _ in range(32):
        persona.energy_updated_at = m.utcnow() - dt.timedelta(minutes=15)
        await chat_module._update_self_energy(_FakeSession(), persona)

    assert persona.self_energy <= 1.0, persona.self_energy


async def test_self_energy_refills_after_two_hours_rest():
    from app.routers import chat as chat_module

    persona = m.PersonaState(user_id=1, self_energy=10.0)
    persona.energy_updated_at = m.utcnow() - dt.timedelta(hours=2)
    await chat_module._update_self_energy(_FakeSession(), persona)

    assert persona.self_energy == 100.0


async def test_short_gap_still_drains_not_refills():
    """29 分钟的间隔还没到休息阈值，应该继续消耗而不是回血。"""
    from app.routers import chat as chat_module

    persona = m.PersonaState(user_id=1, self_energy=50.0)
    persona.energy_updated_at = m.utcnow() - dt.timedelta(minutes=29)
    await chat_module._update_self_energy(_FakeSession(), persona)

    assert persona.self_energy < 50.0
