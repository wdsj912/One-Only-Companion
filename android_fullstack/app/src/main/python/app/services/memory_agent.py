"""
Background Memory Agent — "The Librarian".

The chat agent no longer extracts memories on the hot path. After a turn is
committed, ``spawn`` schedules this agent on the event loop; it re-reads the
recent chat log, asks the LLM which long-term memories to ADD / UPDATE /
DELETE, and applies those operations itself. The chat agent only ever READS
the curated result.

Every pass appends an audit line to ``_memory_agent.log`` at the project root
(the LLM's raw JSON output plus the applied operations). API keys never
appear here. Pinned memories and open threads are deliberately hidden from
the Librarian: pins are user-controlled, open threads have their own
lifecycle (3-day cleanup).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
from typing import Optional

from sqlalchemy import select

from app import models as m
from app.config import BASE_DIR
from app.services.llm_service import LLMError, chat_completion
from app.services.memory_service import OPEN_THREAD_KEY

logger = logging.getLogger(__name__)

LOG_PATH = BASE_DIR / "_memory_agent.log"

RECENT_MESSAGE_COUNT = 6
EXISTING_MEMORY_COUNT = 20
MAX_CONTENT_LEN = 500
MAX_OPS_PER_PASS = 20

_ALLOWED_EMOTIONS = {"joy", "sad", "angry", "anxious"}
_FACT_KEY_RE = re.compile(r"^[a-z0-9_.\-]{1,64}$")

MEMORY_AGENT_PROMPT = """You are the Memory Manager for an AI companion. Your job is to read recent chat logs and decide what long-term memories to ADD, UPDATE, or DELETE.

Current Memories:
{existing_memories}

Recent Chat:
{recent_messages}

Rules:
- Only save meaningful facts, events, preferences, or emotional states.
- Do NOT save filler words, questions, or assistant responses.
- If the user corrects a fact, UPDATE it.
- If a temporary event is over (e.g., 'I have a cold today'), DELETE it after a few days.
- For ADD, include "type": "fact" (stable identity/attribute), "event" (something that happened), or "preference" (likes/dislikes/habits).
- UPDATE and DELETE MUST include the numeric "memory_id" copied from Current Memories. If the fact you want to change is NOT listed in Current Memories, use ADD instead of UPDATE.
- Do NOT ADD facts, events, or preferences that Current Memories already cover — update or skip them instead.

Output exactly a JSON array of operations, no markdown, no commentary. Output [] when nothing is worth remembering.
Example:
[{{"action": "ADD", "type": "fact", "fact_key": "user.name", "content": "The user's name is Azhe", "emotion": "neutral"}},
 {{"action": "UPDATE", "memory_id": 12, "content": "The user likes cats, not dogs"}},
 {{"action": "DELETE", "memory_id": 5}}]"""


# ---------------------------------------------------------------------------
# LLM output parsing — defensive: the model may wrap JSON in prose or fences
# ---------------------------------------------------------------------------

def _extract_json_array(raw: str) -> Optional[list]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _clean_fact_key(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if _FACT_KEY_RE.match(key) else None


def _clean_emotion(value) -> Optional[str]:
    return value if isinstance(value, str) and value in _ALLOWED_EMOTIONS else None


def _hash_key(prefix: str, content: str) -> str:
    """Deterministic key when the model omits one — a repeat mention then
    upserts the same row instead of duplicating it."""
    return f"{prefix}.{hashlib.sha1(content.encode('utf-8')).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Operation handlers — every op is scoped to the current user
# ---------------------------------------------------------------------------

async def _apply_add(session, user_id: int, op: dict) -> str:
    content = str(op.get("content") or "").strip()[:MAX_CONTENT_LEN]
    if not content:
        return "ADD skipped: empty content"
    emotion = _clean_emotion(op.get("emotion"))
    mtype = str(op.get("type") or "").strip().lower()
    fact_key = _clean_fact_key(op.get("fact_key"))

    if mtype == "event":
        fact_key = None  # events carry no key → no unique-constraint collisions
    elif mtype == "preference":
        if fact_key and not fact_key.startswith("preference."):
            fact_key = f"preference.{fact_key}"
        if not fact_key or len(fact_key) > 64:
            fact_key = _hash_key("preference", content)
    elif fact_key is None:
        fact_key = _hash_key("fact", content)

    if fact_key is not None:
        # unique(user_id, layer, fact_key) covers soft-deleted rows too,
        # so upsert/resurrect in place rather than inserting.
        row = (await session.execute(select(m.Memory).where(
            m.Memory.user_id == user_id,
            m.Memory.layer == "semantic",
            m.Memory.fact_key == fact_key,
        ))).scalar_one_or_none()
        if row is not None:
            row.content = content
            row.fact_value = content
            row.emotion = emotion
            row.deleted_at = None
            row.last_referenced_at = m.utcnow()
            return f"ADD→UPDATE fact {fact_key}"
        session.add(m.Memory(
            user_id=user_id, layer="semantic", content=content,
            fact_key=fact_key, fact_value=content, emotion=emotion,
            confidence=0.8, salience=1.0,
        ))
        return f"ADD fact {fact_key}"

    session.add(m.Memory(
        user_id=user_id, layer="episodic", content=content,
        emotion=emotion, intensity=3, salience=0.6,
    ))
    return "ADD event"


async def _find_by_key(session, user_id: int, op: dict):
    """Locate an active memory by fact_key, honouring the preference prefix
    that _apply_add would have applied when the row was created."""
    key = _clean_fact_key(op.get("fact_key"))
    if key is None:
        return None
    candidates = [key]
    if str(op.get("type") or "").strip().lower() == "preference" \
            and not key.startswith("preference."):
        candidates.insert(0, f"preference.{key}")
    return (await session.execute(select(m.Memory).where(
        m.Memory.user_id == user_id,
        m.Memory.fact_key.in_(candidates),
        m.Memory.deleted_at.is_(None),
    ))).scalars().first()


async def _apply_update(session, user_id: int, op: dict) -> str:
    content = str(op.get("content") or "").strip()[:MAX_CONTENT_LEN]
    mid = op.get("memory_id")
    if isinstance(mid, int) and not isinstance(mid, bool):
        if not content:
            return f"UPDATE #{mid} skipped: empty content"
        row = (await session.execute(select(m.Memory).where(
            m.Memory.id == mid,
            m.Memory.user_id == user_id,
            m.Memory.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if row is None:
            return f"UPDATE #{mid} skipped: unknown id"
    else:
        # Models sometimes target a memory by fact_key instead of id.
        if not content:
            return "UPDATE skipped: empty content"
        row = await _find_by_key(session, user_id, op)
        if row is None:
            # The target was never saved (e.g. an earlier pass was
            # rate-limited) — an update to nothing is really an add.
            return await _apply_add(session, user_id, op)
    row.content = content
    if row.layer == "semantic":
        row.fact_value = content
    emotion = _clean_emotion(op.get("emotion"))
    if emotion:
        row.emotion = emotion
    row.last_referenced_at = m.utcnow()
    return f"UPDATE #{row.id}"


async def _apply_delete(session, user_id: int, op: dict) -> str:
    mid = op.get("memory_id")
    if isinstance(mid, int) and not isinstance(mid, bool):
        row = (await session.execute(select(m.Memory).where(
            m.Memory.id == mid,
            m.Memory.user_id == user_id,
            m.Memory.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if row is None:
            return f"DELETE #{mid} skipped: unknown id"
    else:
        # fact_key fallback, same as UPDATE.
        row = await _find_by_key(session, user_id, op)
        if row is None:
            return "DELETE skipped: bad memory_id"
    row.deleted_at = m.utcnow()
    return f"DELETE #{row.id}"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _write_log(*, conversation_id: Optional[int], raw: Optional[str], results: list[str], error: Optional[str] = None) -> None:
    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "conversation_id": conversation_id,
        "llm_output": raw,
        "results": results,
    }
    if error:
        entry["error"] = error
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("memory agent log write failed: %s", exc)


# ---------------------------------------------------------------------------
# Core pass
# ---------------------------------------------------------------------------

async def process_memory(
    session,
    *,
    user_id: int,
    recent_messages: list[dict],
    existing_memories: list[m.Memory],
    conversation_id: Optional[int] = None,
) -> list[str]:
    """One Librarian pass: ask the LLM for memory ops and apply them.

    Returns a per-op result list. LLM/parse failures are logged and produce a
    single diagnostic result — they never raise into the caller.
    """
    mem_view = [
        {
            "id": mem.id,
            "layer": mem.layer,
            "fact_key": mem.fact_key,
            "content": mem.content,
            "saved_at": mem.created_at.strftime("%Y-%m-%d") if mem.created_at else None,
        }
        for mem in existing_memories
    ]
    chat_view = "\n".join(f"{msg.get('role')}: {msg.get('content')}" for msg in recent_messages)
    prompt = MEMORY_AGENT_PROMPT.format(
        existing_memories=json.dumps(mem_view, ensure_ascii=False, indent=1),
        recent_messages=chat_view or "(empty)",
    )
    raw = None
    # The agent fires right after the chat completion — the two back-to-back
    # calls can trip the provider's account-level rate limit (GLM 429/1302).
    # Observed recovery window reached ~110s under sustained load, so back off
    # 5s/15s/30s/60s before giving up; a non-429 failure is raised to the
    # caller's log-and-swallow handler. A permanently failed turn is still
    # re-seen by the next turn's 6-message window.
    _BACKOFF_S = (5, 15, 30, 60)
    for attempt in range(len(_BACKOFF_S) + 1):
        try:
            raw = await chat_completion(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Decide the memory operations now. JSON array only."},
                ],
                temperature=0.2,
                # Thinking-style models (GLM) burn a large hidden token budget
                # before emitting content — 800 truncated the JSON mid-flight.
                max_tokens=3000,
            )
            break
        except LLMError as exc:
            if exc.status_code == 429 and attempt < len(_BACKOFF_S):
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue
            raise

    ops = _extract_json_array(raw)
    results: list[str] = []
    if ops is None:
        results.append("parse error: no JSON array in LLM output")
    else:
        for op in ops[:MAX_OPS_PER_PASS]:
            if not isinstance(op, dict):
                results.append("skipped non-object op")
                continue
            action = str(op.get("action") or "").upper()
            if action == "ADD":
                results.append(await _apply_add(session, user_id, op))
            elif action == "UPDATE":
                results.append(await _apply_update(session, user_id, op))
            elif action == "DELETE":
                results.append(await _apply_delete(session, user_id, op))
            else:
                results.append(f"skipped unknown action: {action!r:.24}")

    _write_log(conversation_id=conversation_id, raw=raw, results=results)
    return results


# ---------------------------------------------------------------------------
# Background entry points
# ---------------------------------------------------------------------------

_CONV_LOCKS: dict[int, asyncio.Lock] = {}


def _conv_lock(conversation_id: int) -> asyncio.Lock:
    """Serialize Librarian runs per conversation — rapid consecutive messages
    would otherwise read the same pre-write state and double-save events.

    Locks bind to the loop of their first await; tests run each case on a
    fresh loop, so a lock bound to a dead loop is recreated here.
    """
    lock = _CONV_LOCKS.get(conversation_id)
    loop = asyncio.get_running_loop()
    # lock._loop is None until first await — only rebind once it was actually
    # bound to a different (possibly closed) loop.
    if lock is None or (lock._loop is not None and lock._loop is not loop):
        lock = asyncio.Lock()
        _CONV_LOCKS[conversation_id] = lock
    return lock


async def run_memory_agent(conversation_id: int, session_factory=None) -> None:
    """Background task body — opens its own DB session so the request-scoped
    session is never touched after the response goes out."""
    from app.database import async_session  # late import: tests swap the factory

    factory = session_factory or async_session
    try:
        async with _conv_lock(conversation_id):
            async with factory() as session:
                conv = await session.get(m.Conversation, conversation_id)
                if conv is None:
                    return
                msgs = list((await session.execute(
                    select(m.Message)
                    .where(m.Message.conversation_id == conversation_id)
                    .order_by(m.Message.id.desc())
                    .limit(RECENT_MESSAGE_COUNT)
                )).scalars().all())
                recent = [{"role": x.role, "content": x.content} for x in reversed(msgs)]
                mems = list((await session.execute(
                    select(m.Memory)
                    .where(
                        m.Memory.user_id == conv.user_id,
                        m.Memory.deleted_at.is_(None),
                        m.Memory.layer.in_(("semantic", "episodic")),
                        (m.Memory.fact_key.is_(None)) | (m.Memory.fact_key != OPEN_THREAD_KEY),
                    )
                    .order_by(m.Memory.created_at.desc())
                    .limit(EXISTING_MEMORY_COUNT)
                )).scalars().all())
                await process_memory(
                    session,
                    user_id=conv.user_id,
                    recent_messages=recent,
                    existing_memories=mems,
                    conversation_id=conversation_id,
                )
                await session.commit()
    except Exception as exc:  # background task — log, never crash the loop
        logger.warning("memory agent failed for conversation %s: %s", conversation_id, exc)
        _write_log(conversation_id=conversation_id, raw=None, results=[], error=str(exc)[:300])


_TASKS: set[asyncio.Task] = set()


def spawn(conversation_id: int) -> None:
    """Fire-and-forget scheduling from the chat hot path. Tasks are tracked in
    a set so the event loop cannot garbage-collect them mid-flight."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (e.g. sync context) — skip the memory pass
    task = loop.create_task(run_memory_agent(conversation_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
