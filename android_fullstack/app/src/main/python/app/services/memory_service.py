"""
Four-layer memory service (spec §4):
  working   — last 20 turns, always in context (lived in the conversation itself)
  episodic  — event-tagged memories with emotion/intensity
  semantic  — structured user facts with confidence, upserted by fact_key
  pinned    — user-pinned messages, protected from consolidation

All memories are soft-deletable. Extraction is the Librarian's job
(``memory_agent``) — this module only stores, retrieves, and consolidates
what has already been curated.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from sqlalchemy import select

from app import models as m


# ---------------------------------------------------------------------------
# Open threads — future commitments worth a proactive follow-up
# ---------------------------------------------------------------------------

_OPEN_THREAD_MARKERS = ("明天", "下周", "等我", "回头", "改天", "下次", "周末", "到时候", "一会儿", "晚点")

OPEN_THREAD_KEY = "open_thread"
OPEN_THREAD_MAX_AGE_DAYS = 3


def extract_open_threads(text: str) -> list[str]:
    """Pull future commitments out of a message ("我明天要去面试" → ["我明天要去面试"]).

    Keyword-based, deterministic, zero-LLM — deliberately separate from
    ``should_save_memory`` so short future plans bypass the filler gate.
    """
    t = (text or "").strip()
    if not t:
        return []
    threads: list[str] = []
    for clause in re.split(r"[。！？!?\n；;]", t):
        c = clause.strip(" ，,")
        if len(c) < 3:
            continue
        if any(k in c for k in _OPEN_THREAD_MARKERS):
            threads.append(c)
    return threads


async def save_open_threads(
    session: Session, user_id: int, threads: list[str], source_message_id: Optional[int] = None
) -> int:
    """Store an open thread as an episodic memory with fact_key="open_thread".

    The (user_id, layer, fact_key) unique constraint covers soft-deleted rows
    too, so there is exactly one physical row per user: the newest thread
    replaces (resurrects) it in place. Returns 1 when stored, else 0.
    """
    if not threads:
        return 0
    row = (await session.execute(
        select(m.Memory).where(
            m.Memory.user_id == user_id,
            m.Memory.layer == "episodic",
            m.Memory.fact_key == OPEN_THREAD_KEY,
        )
    )).scalar_one_or_none()
    newest = threads[-1]
    if row is None:
        session.add(m.Memory(
            user_id=user_id,
            layer="episodic",
            content=newest,
            fact_key=OPEN_THREAD_KEY,
            fact_value=newest,
            source_message_id=source_message_id,
            intensity=3,
            salience=0.6,
        ))
        return 1
    if row.deleted_at is None and row.content == newest:
        return 0
    row.content = newest
    row.fact_value = newest
    row.source_message_id = source_message_id
    row.created_at = m.utcnow()
    row.deleted_at = None
    return 1


async def get_latest_open_thread(session: Session, user_id: int) -> Optional[m.Memory]:
    return (await session.execute(
        select(m.Memory)
        .where(
            m.Memory.user_id == user_id,
            m.Memory.fact_key == OPEN_THREAD_KEY,
            m.Memory.deleted_at.is_(None),
        )
        .order_by(m.Memory.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def cleanup_open_threads(session: Session, user_id: int, older_than_days: int = OPEN_THREAD_MAX_AGE_DAYS) -> int:
    """Soft-delete threads past their window — a stale thread stops nagging."""
    cutoff = m.utcnow() - dt.timedelta(days=older_than_days)
    stale = (await session.execute(
        select(m.Memory).where(
            m.Memory.user_id == user_id,
            m.Memory.fact_key == OPEN_THREAD_KEY,
            m.Memory.deleted_at.is_(None),
            m.Memory.created_at < cutoff,
        )
    )).scalars().all()
    now = m.utcnow()
    for mem in stale:
        mem.deleted_at = now
    return len(stale)


# ---------------------------------------------------------------------------
# Memory CRUD
# ---------------------------------------------------------------------------

async def upsert_semantic_fact(
    session: Session, user_id: int, fact_key: str, fact_value: str, source_message_id: Optional[int] = None
) -> m.Memory:
    """Upsert a semantic fact by (user_id, fact_key). Confidence bumps on repeat mention."""
    existing = (await session.execute(
        select(m.Memory).where(
            m.Memory.user_id == user_id,
            m.Memory.layer == "semantic",
            m.Memory.fact_key == fact_key,
            m.Memory.deleted_at.is_(None),
        )
    )).scalar_one_or_none()

    if existing is not None:
        # Same value → raise confidence; different value → update, keep confidence
        if existing.fact_value == fact_value:
            existing.confidence = min(1.0, existing.confidence + 0.05)
        else:
            existing.fact_value = fact_value
            existing.content = f"{fact_key} = {fact_value}"
            existing.confidence = max(0.6, existing.confidence - 0.05)
        existing.last_referenced_at = m.utcnow()
        return existing

    mem = m.Memory(
        user_id=user_id,
        layer="semantic",
        content=f"{fact_key} = {fact_value}",
        fact_key=fact_key,
        fact_value=fact_value,
        confidence=0.7,
        source_message_id=source_message_id,
        salience=1.0,
    )
    session.add(mem)
    return mem


async def add_episodic_memory(
    session: Session,
    user_id: int,
    content: str,
    source_message_id: Optional[int] = None,
    emotion: Optional[str] = None,
    intensity: int = 3,
) -> m.Memory:
    mem = m.Memory(
        user_id=user_id,
        layer="episodic",
        content=content,
        source_message_id=source_message_id,
        emotion=emotion,
        intensity=intensity,
        salience=float(intensity) / 5.0,
    )
    session.add(mem)
    return mem


async def pin_memory(session: Session, memory_id: int) -> Optional[m.Memory]:
    mem = (await session.execute(select(m.Memory).where(m.Memory.id == memory_id))).scalar_one_or_none()
    if mem is None or mem.deleted_at is not None:
        return None
    mem.pinned = True
    if mem.layer != "pinned":
        mem.layer = "pinned"
    return mem


async def soft_delete_memory(session: Session, memory_id: int) -> bool:
    mem = (await session.execute(select(m.Memory).where(m.Memory.id == memory_id))).scalar_one_or_none()
    if mem is None:
        return False
    mem.deleted_at = m.utcnow()
    return True


async def list_memories(
    session: Session,
    user_id: int,
    layer: Optional[str] = None,
    include_deleted: bool = False,
) -> list[m.Memory]:
    stmt = select(m.Memory).where(m.Memory.user_id == user_id)
    if layer:
        stmt = stmt.where(m.Memory.layer == layer)
    if not include_deleted:
        stmt = stmt.where(m.Memory.deleted_at.is_(None))
    stmt = stmt.order_by(m.Memory.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def retrieve_relevant_memories(
    session: Session,
    user_id: int,
    limit: int = 10,
) -> list[m.Memory]:
    """Three-tier memory retrieval — like a human brain.

    L3 (semantic + pinned): always present, never forgotten, top of the block.
    L2 (episodic):          only within last 14 days, newest first, cap 5.
    Open threads are excluded (they enter via their own prompt block).

    Every retrieved memory gets a small salience boost — "被想起" = 强化,
    exactly like a real brain's reinforcement.
    """
    now = dt.datetime.now(dt.timezone.utc)
    from sqlalchemy import or_, and_

    # ---- L3: semantic + pinned — always there ----
    l3_stmt = select(m.Memory).where(
        m.Memory.user_id == user_id,
        m.Memory.deleted_at.is_(None),
        m.Memory.layer.in_(("semantic", "pinned")),
        (m.Memory.fact_key.is_(None)) | (m.Memory.fact_key != OPEN_THREAD_KEY),
    ).order_by(m.Memory.created_at.desc())
    l3_rows = list((await session.execute(l3_stmt)).scalars().all())

    # ---- L2: episodic — only within 14 days, top 5 ----
    cutoff = now - dt.timedelta(days=14)
    l2_stmt = select(m.Memory).where(
        m.Memory.user_id == user_id,
        m.Memory.deleted_at.is_(None),
        m.Memory.layer == "episodic",
        m.Memory.created_at >= cutoff,
        (m.Memory.fact_key.is_(None)) | (m.Memory.fact_key != OPEN_THREAD_KEY),
    ).order_by(m.Memory.created_at.desc()).limit(5)
    l2_rows = list((await session.execute(l2_stmt)).scalars().all())

    # ---- Reinforcement: every retrieved memory salience += 0.1 ----
    all_rows = l3_rows + l2_rows
    for mem in all_rows:
        mem.salience = min(2.0, (mem.salience or 0.6) + 0.1)
    if all_rows:
        session.add_all(all_rows)
        await session.flush()

    return all_rows


# ---------------------------------------------------------------------------
# Memory consolidation (Layer C) — run after each session.
# ---------------------------------------------------------------------------

async def run_consolidation(session: Session, user_id: int) -> dict:
    """Salience decay, reinforcement promotion, and forgetting.

    salience is the single unified metric:
      - retrieve()          → += 0.1  (被想起 = 强化)
      - consolidation()      → -= 0.02 (自然衰减)
      - episodic + salience > 1.0 → semantic (promotion: 被反复想起 → 沉淀)
      - episodic + 30d old + salience < 0.3 → soft-delete (遗忘曲线)
    """
    now = m.utcnow()
    stats = {"decayed": 0, "promoted": 0, "forgotten": 0, "merged": 0}

    all_active = list((await session.execute(
        select(m.Memory).where(
            m.Memory.user_id == user_id,
            m.Memory.deleted_at.is_(None),
            m.Memory.pinned.is_(False),
        )
    )).scalars().all())

    # Pass 1: decay salience + promotion + forgetting
    for mem in all_active:
        mem.salience = max(0.0, (mem.salience or 1.0) - 0.02)
        if mem.layer != "episodic":
            continue

        age_days = (now.replace(tzinfo=None) - mem.created_at.replace(tzinfo=None)).days

        # Promotion: 被反复想起的 episodic → 沉淀为 semantic
        if mem.salience > 1.0:
            mem.layer = "semantic"
            mem.salience = max(0.5, mem.salience - 0.2)  # reset to reasonable level
            stats["promoted"] += 1
            continue  # 跳过 forgetting 检查，因为已经升了

        # Forgetting curve: 30 天没被想起（salience 自然衰减到 <0.3）
        if age_days >= 30 and mem.salience < 0.3:
            mem.deleted_at = now
            stats["forgotten"] += 1
            continue

        # Low-salience ephemeral cleanup (existing behavior, kept)
        if mem.salience < 0.1:
            mem.deleted_at = now
            stats["decayed"] += 1

    # Pass 2: merge duplicate episodic (same content substring)
    seen_episodic: dict[str, m.Memory] = {}
    for mem in [m for m in all_active if m.layer == "episodic" and not m.deleted_at]:
        key = mem.content[:40].strip()
        if key in seen_episodic:
            seen_episodic[key].salience = max(
                seen_episodic[key].salience or 0, mem.salience or 0
            )
            mem.deleted_at = now
            stats["merged"] += 1
        else:
            seen_episodic[key] = mem

    return stats
