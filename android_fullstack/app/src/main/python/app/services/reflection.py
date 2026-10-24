"""
Layer B — Per-session self-reflection.

After a session (or every 10 turns mid-session), call the LLM once (non-streaming)
with the last N turns and ask: what did you misunderstand? What assumption was wrong?
Store as a dated SelfReflection row.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.services.llm_service import chat_completion


logger = logging.getLogger(__name__)


REFLECTION_PROMPT = """You are {name}. Review the conversation below and answer three questions concisely in Chinese (≤ 200 chars total):
1. 我对用户最明显的误解是什么？
2. 我对用户的哪个假设是错的？
3. 下次我应该做什么不同的事？

Output as three short bullet points, each starting with '- '.

Conversation:
{transcript}
"""


async def run_reflection(
    session: Session,
    user_id: int,
    companion_name: str,
    conversation_id: int,
    llm_available: bool = True,
) -> Optional[m.SelfReflection]:
    """Run one reflection call and persist. Returns the reflection or None."""
    msgs = list((await session.execute(
        select(m.Message).where(m.Message.conversation_id == conversation_id).order_by(m.Message.created_at)
    )).scalars().all())

    if len(msgs) < 4:
        return None

    transcript = "\n".join(f"[{m.role}] {m.content}" for m in msgs)
    user_prompt = REFLECTION_PROMPT.format(name=companion_name, transcript=transcript)

    if not llm_available:
        # Deterministic fallback so tests don't need an LLM key.
        fallback = "- (fallback) 需要真正的 LLM 才能生成有意义的反思\n- 下次先多听再建议\n- 不要急于假设用户感受"
        content = fallback
    else:
        try:
            content = await chat_completion(
                [{"role": "user", "content": user_prompt}],
                temperature=0.4,
                max_tokens=256,
            )
        except Exception as exc:
            logger.warning("reflection LLM call failed: %s", exc)
            return None

    ref = m.SelfReflection(
        user_id=user_id,
        session_turn_count=len(msgs),
        content=content,
        created_at=m.utcnow(),
    )
    session.add(ref)
    return ref


async def get_recent_reflections(session: Session, user_id: int, limit: int = 3) -> list[m.SelfReflection]:
    result = await session.execute(
        select(m.SelfReflection)
        .where(m.SelfReflection.user_id == user_id)
        .order_by(m.SelfReflection.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
