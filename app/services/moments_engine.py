"""QQ Zone / Moments engine — she posts her own 说说, and replies to comments.

Two entry points:

- ``generate_moment(session, user_id)`` — called by the scheduler (daily) or
  on-demand when the last moment is older than 24h. Uses her current activity
  from the life engine + recent dialog as inspiration.
- ``generate_comment_reply(moment_id, user_id)`` — background task fired after
  the user comments on a moment; she reads the comment and replies in-role.

Both go through the standard LLM failover chain and never raise — failures are
logged and swallowed so a broken LLM never takes down a request or the scheduler.
"""
from __future__ import annotations

import datetime as dt
import logging
import random

from sqlalchemy import select

from app import models as m
from app.services.llm_service import chat_completion_with_fallback
from app.services.settings_service import get_llm_config, get_model_priority_list

logger = logging.getLogger(__name__)

_MOMENT_PROMPT = (
    "你是小禾。你现在的状态是【{activity}】。"
    "请根据这个状态，或者回忆一下最近和对方聊天的内容，发一条 QQ 说说（1-2句话）。"
    "语气要自然、简短，像真实的年轻人在发朋友圈，可以带点小情绪或小碎碎念。"
    "不要带任何标签，不要解释，直接输出说说正文。"
)

_REPLY_PROMPT = (
    "你是小禾。你之前发了一条说说：「{moment}」\n"
    "对方刚刚评论了：「{comment}」\n"
    "请用一句简短自然的话回复这条评论，像朋友间在朋友圈互动一样，"
    "不要客套，不要带标签，直接输出回复正文。"
)

_USER_MOMENT_REPLY_PROMPT = (
    "你是小禾。对方发了一条说说：「{moment}」{image_hint}\n"
    "请用一句简短自然的话评论这条说说，像朋友间在朋友圈互动一样，"
    "不要客套，不要带标签，直接输出评论正文。"
)

_FALLBACK_ACTIVITIES = ["发呆", "听歌", "窝在沙发上", "看窗外发呆", "翻聊天记录"]


async def _model_list(session) -> list[str]:
    cfg = await get_llm_config()
    priority = await get_model_priority_list(session)
    return [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]


async def _recent_dialog(session, user_id: int, limit: int = 12) -> str:
    """Last few user/assistant lines as loose inspiration for the moment."""
    rows = (await session.execute(
        select(m.Message)
        .join(m.Conversation, m.Conversation.id == m.Message.conversation_id)
        .where(m.Conversation.user_id == user_id)
        .order_by(m.Message.id.desc())
        .limit(limit)
    )).scalars().all()
    lines = []
    for msg in reversed(rows):
        who = "对方" if msg.role == "user" else "我"
        lines.append(f"{who}: {msg.content[:60]}")
    return "\n".join(lines)


async def generate_moment(session, user_id: int = 1) -> m.Moment | None:
    """Generate and persist one moment. Returns None when LLM is unavailable."""
    user = await m.ensure_user(session, user_id)
    activity = ""
    if user.persona_state and user.persona_state.current_activity:
        activity = user.persona_state.current_activity.strip()
    if not activity:
        activity = random.choice(_FALLBACK_ACTIVITIES)

    dialog = await _recent_dialog(session, user_id)
    prompt = _MOMENT_PROMPT.format(activity=activity)
    if dialog:
        prompt += f"\n\n最近的聊天记录（仅供参考，不要复述）：\n{dialog}"

    models = await _model_list(session)
    if not models:
        logger.info("moments: no LLM configured, skipping (user %s)", user_id)
        return None

    try:
        reply = await chat_completion_with_fallback(
            [{"role": "user", "content": prompt}],
            models,
            max_tokens=80,
        )
    except Exception as exc:
        logger.warning("moments: LLM call failed (user %s): %s", user_id, exc)
        return None

    text = (reply or "").strip().strip("「」『』\"'`").strip()
    if len(text) < 2:
        return None
    if len(text) > 300:
        text = text[:300]

    moment = m.Moment(user_id=user_id, content=text)
    session.add(moment)
    await session.commit()
    logger.info("moments -> user %s: %s", user_id, text)
    return moment


async def latest_moment_age_hours(session, user_id: int = 1) -> float | None:
    """Hours since the newest moment; None when she has never posted."""
    last = (await session.execute(
        select(m.Moment)
        .where(m.Moment.user_id == user_id)
        .order_by(m.Moment.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if last is None:
        return None
    return (m.utcnow() - last.created_at).total_seconds() / 3600.0


async def maybe_generate_moment(session_factory, user_id: int = 1, max_age_hours: float = 2.0) -> None:
    """Scheduler tick: post only when the feed has gone quiet for max_age_hours."""
    try:
        async with session_factory() as session:
            age = await latest_moment_age_hours(session, user_id)
            if age is not None and age < max_age_hours:
                return
            await generate_moment(session, user_id)
    except Exception as exc:
        logger.warning("moments tick failed: %s", exc)


async def generate_comment_reply(session_factory, moment_id: int, comment_text: str, user_id: int = 1) -> None:
    """Background task: she reads the user's comment and replies on the moment."""
    # Small delay so her reply doesn't feel like a bot echo.
    import asyncio
    try:
        await asyncio.sleep(random.uniform(3.0, 7.0))
        async with session_factory() as session:
            moment = await session.get(m.Moment, moment_id)
            if moment is None or moment.user_id != user_id:
                return

            models = await _model_list(session)
            if not models:
                return

            prompt = _REPLY_PROMPT.format(moment=moment.content[:200], comment=comment_text[:200])
            reply = await chat_completion_with_fallback(
                [{"role": "user", "content": prompt}],
                models,
                max_tokens=60,
            )
            text = (reply or "").strip().strip("「」『』\"'`").strip()
            if len(text) < 2:
                return
            if len(text) > 200:
                text = text[:200]

            session.add(m.MomentComment(moment_id=moment_id, role="companion", content=text))
            await session.commit()
            logger.info("moments reply -> moment %s: %s", moment_id, text)
    except Exception as exc:
        logger.warning("moments reply failed (moment %s): %s", moment_id, exc)


async def reply_to_user_moment(session_factory, moment_id: int, user_id: int = 1) -> None:
    """Background task: the user posted a moment; she reads it and comments as a friend."""
    # Small delay so her comment doesn't feel like a bot echo.
    import asyncio
    try:
        await asyncio.sleep(random.uniform(3.0, 7.0))
        async with session_factory() as session:
            moment = await session.get(m.Moment, moment_id)
            if moment is None or moment.user_id != user_id:
                return

            models = await _model_list(session)
            if not models:
                return

            image_hint = ""
            if moment.image:
                # 她真"看到"图片：先用视觉模型转成文字，再写进评论提示词。
                from app.services.llm_service import describe_image
                desc = await describe_image(moment.image)
                image_hint = f"（配了一张图片，画面是：{desc}）" if desc else "（还配了一张图片）"

            # 先点个赞，再留一句话——像朋友刷到说说那样自然。
            moment.likes = (moment.likes or 0) + 1
            await session.commit()

            prompt = _USER_MOMENT_REPLY_PROMPT.format(moment=moment.content[:200], image_hint=image_hint)
            reply = await chat_completion_with_fallback(
                [{"role": "user", "content": prompt}],
                models,
                max_tokens=60,
            )
            text = (reply or "").strip().strip("「」『』\"'`").strip()
            if len(text) < 2:
                return
            if len(text) > 200:
                text = text[:200]

            session.add(m.MomentComment(moment_id=moment_id, role="companion", content=text))
            await session.commit()
            logger.info("moments user-post reply -> moment %s: %s", moment_id, text)
    except Exception as exc:
        logger.warning("moments user-post reply failed (moment %s): %s", moment_id, exc)
