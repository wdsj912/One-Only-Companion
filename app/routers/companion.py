"""Companion + proactive preferences routes."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import _DATA_DIR
from app.database import get_session
from app.services.persona_drift import get_or_create_persona, apply_session_drift


router = APIRouter(prefix="/api/companion", tags=["companion"])


class PersonaTrait(BaseModel):
    label: str
    value: int


class CompanionState(BaseModel):
    user_id: int
    name: str
    mood: str
    energy: int
    intimacy: int
    relation_stage: str
    # 她的性格只有一份来源：固定的 15 维配方。界面卡片就展示这份配方里
    # 最有辨识度的几条，免得跟提示词里的人格各说各话。
    persona_profile: list[PersonaTrait] = []
    warmth: float
    playfulness: float
    directness: float
    protectiveness: float
    morning_checkin: bool
    evening_checkin: bool
    muted: bool
    current_activity: Optional[str] = None
    avatar_url: Optional[str] = None
    # 情绪面板：效价（-1~1）与复合情绪标签（时间平复 + 生物钟联合算出）
    valence: float = 0.0
    mood_label: str = "平静"


class UpdateCompanionRequest(BaseModel):
    name: Optional[str] = None
    morning_checkin: Optional[bool] = None
    evening_checkin: Optional[bool] = None
    muted: Optional[bool] = None


def _persona_profile(persona) -> list[PersonaTrait]:
    """Top 4 dimensions of the fixed recipe (base, not context-shifted)."""
    from app.services import persona_recipe as pr

    recipe, _ = pr.get_or_create_recipe(persona.user_id, persona.recipe)
    ranked = pr.ranked_dimensions(recipe)
    return [PersonaTrait(label=dim, value=val) for dim, val in ranked[:4]]


async def _load(user_id: int, session: AsyncSession) -> m.User:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(m.User).options(selectinload(m.User.persona_state)).where(m.User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Assign the relationship directly — reading it after flush would emit
        # implicit lazy IO, which raises MissingGreenlet in the async session.
        user = m.User(id=user_id)
        user.persona_state = m.PersonaState()
        session.add(user)
        await session.flush()
        return user
    if user.persona_state is None:
        user.persona_state = m.PersonaState(user_id=user.id)
        session.add(user.persona_state)
    return user


@router.get("", response_model=CompanionState)
async def get_state(user_id: int = 1, session: AsyncSession = Depends(get_session)):
    user = await _load(user_id, session)
    # 精力随生物钟变化：深夜压帽/早晨睡醒回血/白天缓慢恢复。
    # 放在这里（而不只是发消息时）是为了让面板一打开就显示当前真实值。
    try:
        from app.services.energy import recalc_energy
        new_energy, _ = recalc_energy(user.energy, user.last_interaction_at)
        if user.energy is None or abs(new_energy - user.energy) > 0.05:
            user.energy = new_energy
            session.add(user)
            await session.commit()
    except Exception:
        pass
    # Refresh virtual life state — best-effort, never blocks the response.
    try:
        from app.services.life_engine import update_life_activity
        await update_life_activity(session, user.id)
    except Exception:
        pass
    activity = getattr(user.persona_state, "current_activity", None) if user.persona_state else None

    # 时间平复效应：情绪随沉默向 0 衰减，打开面板就能看到她已经"缓过来了"
    valence = 0.0
    mood_label = "平静"
    try:
        from app.services.emotion_kernel import (
            apply_time_decay_to_formulation_json,
            compound_mood_label,
        )
        new_json, valence = apply_time_decay_to_formulation_json(
            user.formulation_json, user.last_interaction_at
        )
        mood_label = compound_mood_label(valence, user.energy if user.energy is not None else 70)
        if new_json is not None and new_json != user.formulation_json:
            user.formulation_json = new_json
            session.add(user)
            await session.commit()
    except Exception:
        pass
    return CompanionState(
        user_id=user.id,
        name=user.companion_name,
        mood=user.mood,
        energy=user.energy,
        intimacy=user.intimacy,
        relation_stage=user.relation_stage or "exploratory",
        persona_profile=_persona_profile(user.persona_state),
        warmth=user.persona_state.warmth,
        playfulness=user.persona_state.playfulness,
        directness=user.persona_state.directness,
        protectiveness=user.persona_state.protectiveness,
        morning_checkin=user.morning_checkin,
        evening_checkin=user.evening_checkin,
        muted=user.muted,
        current_activity=activity,
        avatar_url=user.avatar_url,
        valence=valence,
        mood_label=mood_label,
    )


@router.patch("", response_model=CompanionState)
async def update(req: UpdateCompanionRequest, user_id: int = 1, session: AsyncSession = Depends(get_session)):
    user = await _load(user_id, session)
    if req.name is not None:
        user.companion_name = req.name
    if req.morning_checkin is not None:
        user.morning_checkin = req.morning_checkin
    if req.evening_checkin is not None:
        user.evening_checkin = req.evening_checkin
    if req.muted is not None:
        user.muted = req.muted
    await session.commit()
    activity = getattr(user.persona_state, "current_activity", None) if user.persona_state else None
    return CompanionState(
        user_id=user.id,
        name=user.companion_name,
        mood=user.mood,
        energy=user.energy,
        intimacy=user.intimacy,
        relation_stage=user.relation_stage or "exploratory",
        persona_profile=_persona_profile(user.persona_state),
        warmth=user.persona_state.warmth,
        playfulness=user.persona_state.playfulness,
        directness=user.persona_state.directness,
        protectiveness=user.persona_state.protectiveness,
        morning_checkin=user.morning_checkin,
        evening_checkin=user.evening_checkin,
        muted=user.muted,
        current_activity=activity,
        avatar_url=user.avatar_url,
    )


# --- 头像 -----------------------------------------------------------------
# Android 包内 static 目录是只读的，所以头像统一落在可写数据目录
# （桌面为项目根，Android 为应用私有 filesDir），再由下面的 GET 提供。
_AVATAR_DIR = Path(_DATA_DIR) / "avatars"


class AvatarIn(BaseModel):
    image: str


@router.post("/avatar")
async def upload_avatar(
    body: AvatarIn,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    mm = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", body.image.strip(), re.S)
    if not mm:
        raise HTTPException(status_code=400, detail="invalid image payload")
    try:
        data = base64.b64decode(mm.group(2), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64")
    if len(data) > 2_000_000:
        raise HTTPException(status_code=413, detail="avatar too large")
    _AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"png": "png", "webp": "webp"}.get(mm.group(1), "jpg")
    fname = f"user_{user_id}.{ext}"
    (_AVATAR_DIR / fname).write_bytes(data)
    # 旧扩展名的残留文件清掉，避免浏览器缓存住旧头像。
    for old in _AVATAR_DIR.glob(f"user_{user_id}.*"):
        if old.name != fname:
            try:
                old.unlink()
            except OSError:
                pass
    user = await _load(user_id, session)
    user.avatar_url = f"/api/companion/avatar/{fname}"
    await session.commit()
    return {"avatar_url": user.avatar_url}


@router.get("/avatar/{fname}")
async def get_avatar(fname: str):
    safe = re.sub(r"[^A-Za-z0-9_.]", "", fname)
    path = _AVATAR_DIR / safe
    if not safe or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path))


@router.post("/apply-drift")
async def force_drift(
    session: AsyncSession = Depends(get_session),
    user_id: int = 1,
    playfulness: float = 0.0,
    warmth: float = 0.0,
    directness: float = 0.0,
    protectiveness: float = 0.0,
):
    """Manual hook — useful for tests + demos to show the trait vector moving."""
    user = await _load(user_id, session)
    persona = await get_or_create_persona(session, user.id)
    await apply_session_drift(
        session,
        persona,
        {"playfulness": playfulness, "warmth": warmth, "directness": directness, "protectiveness": protectiveness},
    )
    await session.commit()
    return {"warmth": persona.warmth, "playfulness": persona.playfulness, "directness": persona.directness, "protectiveness": persona.protectiveness}
