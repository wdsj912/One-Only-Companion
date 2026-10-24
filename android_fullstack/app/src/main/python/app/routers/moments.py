"""QQ Zone / Moments routes — her feed, likes, and comment interactions."""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.database import get_session, async_session as session_factory
from app.services.moments_engine import generate_comment_reply, reply_to_user_moment

router = APIRouter(prefix="/api/moments", tags=["moments"])

# Strong references to in-flight background tasks so the GC can't reap them.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


class CommentOut(BaseModel):
    id: int
    moment_id: int
    role: str
    content: str
    created_at: dt.datetime


class MomentOut(BaseModel):
    id: int
    content: str
    image: Optional[str] = None
    created_at: dt.datetime
    likes: int
    is_liked_by_user: bool
    comments: list[CommentOut]


class CommentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class MomentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    image: Optional[str] = Field(default=None, max_length=3_000_000)


async def _owned_moment(session: AsyncSession, moment_id: int, user_id: int) -> m.Moment:
    moment = await session.get(m.Moment, moment_id)
    # 404 (not 403) so a wrong user can't probe the existence of other feeds.
    if moment is None or moment.user_id != user_id:
        raise HTTPException(404, "moment not found")
    return moment


@router.get("", response_model=list[MomentOut])
async def list_moments(
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    moments = (await session.execute(
        select(m.Moment)
        .where(m.Moment.user_id == user_id)
        .order_by(m.Moment.created_at.desc(), m.Moment.id.desc())
        .limit(100)
    )).scalars().all()
    if not moments:
        return []

    comments = (await session.execute(
        select(m.MomentComment)
        .where(m.MomentComment.moment_id.in_([mo.id for mo in moments]))
        .order_by(m.MomentComment.created_at, m.MomentComment.id)
    )).scalars().all()

    by_moment: dict[int, list[m.MomentComment]] = {}
    for c in comments:
        by_moment.setdefault(c.moment_id, []).append(c)

    return [
        MomentOut(
            id=mo.id,
            content=mo.content,
            image=mo.image,
            created_at=mo.created_at,
            likes=mo.likes,
            is_liked_by_user=mo.is_liked_by_user,
            comments=[CommentOut(**c.__dict__) for c in by_moment.get(mo.id, [])],
        )
        for mo in moments
    ]


@router.post("/{moment_id}/like", response_model=MomentOut)
async def toggle_like(
    moment_id: int,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    moment = await _owned_moment(session, moment_id, user_id)
    moment.is_liked_by_user = not moment.is_liked_by_user
    moment.likes = max(0, moment.likes + (1 if moment.is_liked_by_user else -1))
    await session.commit()

    comments = (await session.execute(
        select(m.MomentComment)
        .where(m.MomentComment.moment_id == moment.id)
        .order_by(m.MomentComment.created_at, m.MomentComment.id)
    )).scalars().all()
    return MomentOut(
        id=moment.id,
        content=moment.content,
        image=moment.image,
        created_at=moment.created_at,
        likes=moment.likes,
        is_liked_by_user=moment.is_liked_by_user,
        comments=[CommentOut(**c.__dict__) for c in comments],
    )


@router.post("", response_model=MomentOut, status_code=201)
async def create_moment(
    body: MomentIn,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    text = body.content.strip()
    if not text:
        raise HTTPException(422, "empty moment")

    moment = m.Moment(user_id=user_id, content=text, image=body.image)
    session.add(moment)
    await session.commit()
    await session.refresh(moment)

    # She reads it and comments in the background — never blocking this request.
    task = asyncio.create_task(reply_to_user_moment(session_factory, moment.id, user_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return MomentOut(
        id=moment.id,
        content=moment.content,
        image=moment.image,
        created_at=moment.created_at,
        likes=moment.likes,
        is_liked_by_user=moment.is_liked_by_user,
        comments=[],
    )


@router.post("/{moment_id}/comment", response_model=CommentOut, status_code=201)
async def add_comment(
    moment_id: int,
    body: CommentIn,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    moment = await _owned_moment(session, moment_id, user_id)
    text = body.content.strip()
    if not text:
        raise HTTPException(422, "empty comment")

    comment = m.MomentComment(moment_id=moment.id, role="user", content=text)
    session.add(comment)
    await session.commit()
    await session.refresh(comment)

    # She replies in the background — her own LLM turn, never blocking this request.
    task = asyncio.create_task(generate_comment_reply(session_factory, moment.id, text, user_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return CommentOut(**comment.__dict__)
