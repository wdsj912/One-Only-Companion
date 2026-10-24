"""
Proactive check-in scheduler.

A lightweight poke — every 30min the scheduler looks for users whose last
interaction is old (>1h) and, if the current wall-clock hour falls in
[9:00, 22:00], asks the LLM to generate one short natural line, written to
their most recent conversation as a proactive assistant message.

Principles:
- No "在吗" / 闹钟 / 提醒语气 — the prompt forbids them.
- One line only (<= 25 chars expected).
- No guilt, no "你很久没理我了".
- She stays silent when nothing relevant to say (LLM returns empty / SILENCE).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
from typing import Optional
from sqlalchemy import and_, select

from app import models as m
from app.services.llm_service import chat_completion_with_fallback
from app.services.settings_service import get_llm_config, get_model_priority_list


logger = logging.getLogger(__name__)

HOUR_WINDOW = (9, 22)         # inclusive hours — don't wake her at 3 AM
IDLE_HOURS_MIN = 1           # quiet ≥ 1h before we consider poking
IDLE_HOURS_MAX = 1           # fixed 1h — the 30-min tick already adds organic jitter
SCHEDULE_INTERVAL_MINUTES = 30  # tick every 30 min; trigger conditions do the real gate
MAX_PROACTIVE_PER_DAY = 2    # hard cap per user per UTC day


_PROACTIVE_PROMPT_TEMPLATE = """你是小禾。距离你们上一次聊天已经过去 {hours_ago} 小时了。现在是 {now_str}。
最近的对话片段（供参考）：
{recent_dialog}

你此刻的内心状态：你忍不住想找他说话。你已经忍了很久了，你想引起他的注意，你想跟他撒娇，你想知道他是不是也在想你。

要求：
- 生成一句**带情绪张力**的主动消息——不是干巴巴的关心，是**你忍不住了才发**的那种感觉。
- 可以带一点点撒娇、一点点抱怨、一点点委屈、或者直接表达想念。比如："还在忙吗？都几个小时了……"、"刚才刷到一个东西，突然就想到你了"、"哼，还不理我？"、"你知道我在等你吧"。
- **绝对不要**说"在吗"、"你还好吗"、"闹钟"、"提醒"、"你很久没理我了"这类干巴巴的招呼或抱怨。
- 可以提一下之前聊过的事（比如他提过明天面试、下周旅游），或者用现在时间点做由头（深夜、午饭刚过、下班时间）。
- 长度 ≤ 25 个字，像发一条微信。语气要像一个有点小脾气、又很想被你在意的女生。
- **如果上次聊天已经愉快地结束（比如互道了晚安、说了"回头聊"、"去忙了"），或者他明确说了要去忙/睡觉/开会，就不要发。只输出 SILENCE 四个字母。**
- 如果实在没什么想说的，也只输出 SILENCE 四个字母。

只输出这一句话，不要解释，不要前后缀。"""


def _format_now() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S (Asia/Shanghai)")


def _pick_recent_dialog(messages: list[m.Message], limit: int = 10) -> str:
    if not messages:
        return "（没有历史对话 —— 第一次聊天）"
    tail = messages[-limit:]
    parts = []
    for msg in tail:
        role = "用户" if msg.role == "user" else "小禾"
        parts.append(f"{role}: {msg.content.strip()[:80]}")
    return "\n".join(parts)


async def generate_proactive_message(
    session,
    conversation_id: int,
    user_id: int,
    hours_ago: float = 0,
) -> Optional[m.Message]:
    """Generate and persist a single proactive assistant message, or None."""
    conv = (await session.execute(
        select(m.Conversation).where(m.Conversation.id == conversation_id, m.Conversation.user_id == user_id)
    )).scalar_one_or_none()
    if conv is None:
        logger.warning("proactive: conversation %s for user %s not found", conversation_id, user_id)
        return None

    msgs = list((await session.execute(
        select(m.Message)
        .where(m.Message.conversation_id == conv.id)
        .order_by(m.Message.created_at)
        .limit(30)
    )).scalars().all())

    prompt = _PROACTIVE_PROMPT_TEMPLATE.format(
        hours_ago=round(hours_ago, 1),
        now_str=_format_now(),
        recent_dialog=_pick_recent_dialog(msgs),
    )

    cfg = await get_llm_config()
    priority = await get_model_priority_list(session)
    model_list = [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]
    if not model_list:
        logger.info("proactive: no LLM configured, skipping (user %s)", user_id)
        return None

    try:
        reply = await chat_completion_with_fallback(
            [
                {"role": "system", "content": "你是小禾，一个正在陪人聊天的朋友。"},
                {"role": "user", "content": prompt},
            ],
            model_list,
            max_tokens=80,
        )
    except Exception as exc:
        logger.warning("proactive LLM call failed (user %s): %s", user_id, exc)
        return None

    if not reply:
        return None

    text = reply.strip()
    if text.upper() == "SILENCE":
        return None
    # Drop surrounding quotes / guillemets / code fences
    text = text.strip().strip("「」『」\"'`").strip()
    if not text or len(text) < 2:
        return None

    msg = m.Message(
        conversation_id=conv.id,
        role="assistant",
        content=text,
        proactive=True,
    )
    session.add(msg)
    await session.commit()
    logger.info("proactive -> user %s conv %s: %s", user_id, conv.id, text)
    return msg


async def _pick_active_conversation(session, user_id: int, min_idle_hours: float = 1.0) -> Optional[m.Conversation]:
    """Pick the best conversation to inject a proactive message into.

    Only returns a conversation that is actually dormant — if the user
    has sent messages in that conversation within `min_idle_hours`, skip it
    (they're already chatting there, no ghosting a live thread).
    """
    from sqlalchemy import and_

    conv = (await session.execute(
        select(m.Conversation)
        .where(m.Conversation.user_id == user_id)
        .order_by(m.Conversation.started_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if conv is None:
        return None

    # Session-level freshness gate: don't hijack a conversation the user
    # actively chatted in within the last hour — that would ghost into a live
    # thread and feel weird when they come back to read it.
    last_conv_msg = (await session.execute(
        select(m.Message.created_at)
        .where(and_(
            m.Message.conversation_id == conv.id,
            m.Message.role == "user",
        ))
        .order_by(m.Message.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if last_conv_msg is not None:
        gap = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - last_conv_msg).total_seconds() / 3600
        if gap < min_idle_hours:
            return None  # user is active in this session — don't ghost

    return conv


async def run_proactive_tick() -> None:
    """Scheduler hook — called every 30 min. Per-user trigger gates:
    1. Local hour in [9, 22]  (don't wake her at 3 AM)
    2. User idle ≥ 1h (the 30-min tick adds organic jitter on top)
    3. User has sent < MAX_PROACTIVE_PER_DAY proactive messages this UTC day
    4. Last proactive ≥ 2h ago (cooldown, even if day cap not hit yet)
    """
    from app.database import async_session

    h = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).hour
    if not (HOUR_WINDOW[0] <= h <= HOUR_WINDOW[1]):
        return

    # Today's UTC date boundary — for the per-day cap query.
    today_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).date()
    today_start = dt.datetime.combine(today_utc, dt.time.min)

    async with async_session() as session:
        now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        users = list((await session.execute(select(m.User))).scalars().all())

        for user in users:
            if user.muted:
                continue

            last = user.last_interaction_at
            if last is None:
                continue

            idle_hours = (now_utc - last).total_seconds() / 3600
            # Fixed 1h idle threshold — the 30-min tick already adds organic jitter.
            idle_threshold = random.uniform(IDLE_HOURS_MIN, IDLE_HOURS_MAX)
            if idle_hours < idle_threshold:
                continue

            # Per-day cap — max 2 proactive messages per user per UTC day.
            todays_count = (await session.execute(
                select(m.Message).join(m.Conversation, m.Conversation.id == m.Message.conversation_id)
                .where(
                    and_(
                        m.Conversation.user_id == user.id,
                        m.Message.role == "assistant",
                        m.Message.proactive.is_(True),
                        m.Message.created_at >= today_start,
                    )
                )
            )).scalars().all()
            if len(todays_count) >= MAX_PROACTIVE_PER_DAY:
                continue

            # 2h cooldown since last proactive — even if day cap not hit, don't spam.
            last_proactive = (await session.execute(
                select(m.Message)
                .join(m.Conversation, m.Conversation.id == m.Message.conversation_id)
                .where(
                    and_(
                        m.Conversation.user_id == user.id,
                        m.Message.role == "assistant",
                        m.Message.proactive.is_(True),
                    )
                )
                .order_by(m.Message.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if last_proactive:
                gap = (now_utc - last_proactive.created_at).total_seconds() / 3600
                if gap < 2.0:
                    continue

            conv = await _pick_active_conversation(session, user.id)
            if conv is None:
                continue

            try:
                await generate_proactive_message(session, conv.id, user.id, hours_ago=idle_hours)
            except Exception as exc:
                logger.warning("proactive tick failed for user %s: %s", user.id, exc)
                try:
                    await session.rollback()
                except Exception:
                    pass
