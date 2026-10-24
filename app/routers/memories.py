"""Memory management routes — user-visible, full CRUD on every memory."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.database import get_session
from app.services.memory_service import list_memories, pin_memory, soft_delete_memory


router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryOut(BaseModel):
    id: int
    layer: str
    content: str
    emotion: Optional[str]
    intensity: int
    fact_key: Optional[str]
    fact_value: Optional[str]
    confidence: float
    salience: float
    pinned: bool
    created_at: dt.datetime
    deleted_at: Optional[dt.datetime]


@router.get("", response_model=list[MemoryOut])
async def get_memories(
    user_id: int = 1,
    layer: Optional[str] = None,
    include_deleted: bool = False,
    session: AsyncSession = Depends(get_session),
):
    mems = await list_memories(session, user_id, layer=layer, include_deleted=include_deleted)
    return [MemoryOut(**m.__dict__) for m in mems]


@router.delete("/{memory_id}")
async def delete_memory(memory_id: int, session: AsyncSession = Depends(get_session)):
    ok = await soft_delete_memory(session, memory_id)
    if not ok:
        raise HTTPException(404, "memory not found")
    return {"ok": True}


@router.post("/{memory_id}/pin")
async def pin(memory_id: int, session: AsyncSession = Depends(get_session)):
    mem = await pin_memory(session, memory_id)
    if mem is None:
        raise HTTPException(404, "memory not found")
    return MemoryOut(**mem.__dict__)
