"""Memory upsert, dedup, and layer isolation tests.

Regex fact extraction was removed with the Dual-Agent split — what to
remember is the Librarian's decision now (see test_memory_agent.py).
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

from app import models as m
from app.database import Base
from app.services.memory_service import (
    upsert_semantic_fact,
    run_consolidation,
    pin_memory,
    soft_delete_memory,
)


@pytest.fixture
async def db_session():
    """Fresh in-memory SQLite, one session per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed user + persona
    async with factory() as session:
        u = m.User(id=1)
        session.add(u)
        await session.commit()

    session = factory()
    yield session
    await session.close()
    await engine.dispose()


class TestUpsert:
    async def test_first_insert(self, db_session):
        mem = await upsert_semantic_fact(db_session, 1, "user.name", "林小夏")
        await db_session.flush()
        assert mem.id is not None
        assert mem.layer == "semantic"
        assert mem.confidence == 0.7
        await db_session.commit()

    async def test_same_value_bumps_confidence(self, db_session):
        m1 = await upsert_semantic_fact(db_session, 1, "user.name", "林小夏")
        conf1 = m1.confidence
        await db_session.flush()
        m2 = await upsert_semantic_fact(db_session, 1, "user.name", "林小夏")
        assert m2.id == m1.id
        assert m2.confidence > conf1

    async def test_different_value_upserts_same_row(self, db_session):
        m1 = await upsert_semantic_fact(db_session, 1, "user.name", "林小夏")
        m2 = await upsert_semantic_fact(db_session, 1, "user.name", "张小夏")
        assert m2.id == m1.id
        assert m2.fact_value == "张小夏"


class TestConsolidation:
    async def test_pinned_memories_never_decay(self, db_session):
        pinned = m.Memory(
            user_id=1, layer="episodic", content="我最开心的那天",
            pinned=True, salience=0.12,
        )
        decaying = m.Memory(
            user_id=1, layer="episodic", content="我早上吃了包子",
            pinned=False, salience=0.12,
        )
        db_session.add_all([pinned, decaying])
        await db_session.flush()

        # Each consolidation pass subtracts 0.02 salience; due to float math,
        # 0.12 - 0.02 = 0.0999... which is < 0.1 — so it deletes on the first pass.
        stats = await run_consolidation(db_session, 1)
        assert stats["decayed"] == 1  # unpinned memory decays; pinned untouched

        await db_session.refresh(pinned)
        await db_session.refresh(decaying)
        assert pinned.deleted_at is None
        assert decaying.deleted_at is not None


class TestPinning:
    async def test_pin_sets_pinned_true_and_layer(self, db_session):
        mem = m.Memory(user_id=1, layer="episodic", content="x")
        db_session.add(mem)
        await db_session.flush()

        got = await pin_memory(db_session, mem.id)
        assert got.pinned is True
        assert got.layer == "pinned"

    async def test_soft_delete(self, db_session):
        mem = m.Memory(user_id=1, layer="episodic", content="x")
        db_session.add(mem)
        await db_session.flush()
        mem_id = mem.id
        ok = await soft_delete_memory(db_session, mem_id)
        assert ok is True
        await db_session.flush()  # persist the soft-delete so refresh picks it up
        await db_session.refresh(mem)
        assert mem.deleted_at is not None
