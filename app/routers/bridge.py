"""OpenAI-compatible bridge — lets external bot frameworks (AstrBot 等) 以标准
/v1/chat/completions 协议直接和小禾对话。

内部直接复用 /api/chat/send 的完整链路（情感内核、记忆、人格、安全闸门），
QQ 上的每一句和 App 里的每一句进入同一个灵魂、同一条时间线。

鉴权：Authorization: Bearer <访问令牌> 或 X-API-Token 头（令牌见 .auth_token 文件）。
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.database import get_session
from app.security import token_matches


router = APIRouter(tags=["bridge"])

MAX_BRIDGE_TEXT_LEN = 4000


class ChatCompletionIn(BaseModel):
    model: Optional[str] = None
    messages: list[dict]
    stream: Optional[bool] = False


async def _bridge_auth(
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None),
) -> None:
    """接受 X-API-Token 或 OpenAI 风格的 Authorization: Bearer 头。"""
    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
    if token_matches(x_api_token) or token_matches(bearer):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


async def _latest_conversation(session: AsyncSession, user_id: int) -> m.Conversation:
    """QQ 消息落进最近一条会话：和手机端共享同一条时间线，她两边都记得。"""
    conv = (await session.execute(
        select(m.Conversation).where(m.Conversation.user_id == user_id)
        .order_by(m.Conversation.id.desc()).limit(1)
    )).scalar_one_or_none()
    if conv is None:
        conv = m.Conversation(user_id=user_id)
        session.add(conv)
        await session.flush()
    return conv


@router.post("/v1/chat/completions", dependencies=[Depends(_bridge_auth)])
async def chat_completions(body: ChatCompletionIn, session: AsyncSession = Depends(get_session)):
    # 只取最后一条 user 消息；对方框架传来的 system prompt 全部忽略——
    # 人格只能来自我们自己的 STATIC_PERSONA，防止外部系统提示词覆盖人格。
    text = ""
    for msg in reversed(body.messages):
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            if isinstance(content, list):  # OpenAI 多模态分片格式，只取文本
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            text = str(content)
            break
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="no user message")
    if len(text) > MAX_BRIDGE_TEXT_LEN:
        raise HTTPException(status_code=422, detail="message too long")

    from app.routers.chat import SendMessageRequest, send_message, _active_user

    await _active_user(session, user_id=1)  # 先确保用户存在，否则会话外键失败
    conv = await _latest_conversation(session, user_id=1)
    result = await send_message(
        SendMessageRequest(user_id=1, conversation_id=conv.id, text=text),
        session,
    )
    reply = result.get("reply") or "……"

    return {
        "id": "chatcmpl-xiaohe",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model or "xiaohe",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
