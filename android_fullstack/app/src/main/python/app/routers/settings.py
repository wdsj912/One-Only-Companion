"""In-app API settings endpoints (LLM endpoint/key/model)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.services import settings_service
from app.services.llm_service import chat_completion_with_fallback_detail
from app.services.net_guard import UnsafeURLError, ensure_public_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

MAX_BASE_URL_LEN = 500
MAX_API_KEY_LEN = 512
MAX_MODEL_LEN = 128
MAX_MODEL_ITEM_LEN = 100


class LLMSettingsIn(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    vision_model: Optional[str] = None
    vision_base_url: Optional[str] = None
    vision_api_key: Optional[str] = None
    model_priority_list: Optional[list[str]] = None


def _reject_control(value: str, field: str) -> None:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise HTTPException(status_code=400, detail=f"{field} must not contain control characters")


async def _normalize_base_url(value: Optional[str]) -> Optional[str]:
    """Reject anything that is not a plain public http(s) endpoint.

    Without this, the settings endpoint could be pointed at an attacker host
    (or an internal service) and the stored API key would be sent there.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return ""
    if len(v) > MAX_BASE_URL_LEN:
        raise HTTPException(status_code=400, detail=f"base_url must be at most {MAX_BASE_URL_LEN} characters")
    _reject_control(v, "base_url")
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="base_url must not embed credentials")
    try:
        # Best effort here (an offline host must still be savable); the
        # authoritative check runs before every outbound request.
        await ensure_public_base_url(v, require_dns=False)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return v


def _normalize_api_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) > MAX_API_KEY_LEN:
        raise HTTPException(status_code=400, detail=f"api_key must be at most {MAX_API_KEY_LEN} characters")
    _reject_control(value, "api_key")
    return value


def _normalize_model(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if len(v) > MAX_MODEL_LEN:
        raise HTTPException(status_code=400, detail=f"model must be at most {MAX_MODEL_LEN} characters")
    _reject_control(v, "model")
    return v


def _sanitize_error(message: str, api_key: str) -> str:
    """Strip the raw key from any upstream error before returning it."""
    if api_key and api_key in message:
        message = message.replace(api_key, settings_service.mask_key(api_key))
    return message[:300]


def _sanitize_attempts(attempts: list[dict], api_key: str) -> list[dict]:
    return [
        {"model": a.get("model", ""), "error": _sanitize_error(a.get("error", ""), api_key)}
        for a in attempts
    ]


@router.get("")
async def read_settings(session: AsyncSession = Depends(get_session)) -> dict:
    cfg = await settings_service.get_llm_config(session)
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_masked": settings_service.mask_key(cfg["api_key"]),
        "configured": bool(cfg["api_key"]),
        "vision_model": cfg.get("vision_model", ""),
        "vision_base_url": cfg.get("vision_base_url", ""),
        "vision_api_key_masked": settings_service.mask_key(cfg.get("vision_api_key", "")),
        "model_priority_list": await settings_service.get_model_priority_list(session),
    }


def _db_error_brief(exc: Optional[BaseException]) -> str:
    """Driver-level reason only, never the SQL statement.

    ``str(exc)`` embeds the compiled statement *and its bound parameters*
    (which include the API key), so only ``orig`` — the DBAPI message, e.g.
    "database is locked" / "no such column: updated_at" — may be shown.
    """
    if exc is None:
        return "unknown"
    orig = getattr(exc, "orig", None)
    if orig is not None:
        return f"{type(orig).__name__}: {str(orig)[:120]}"
    return type(exc).__name__


@router.post("")
async def update_settings(
    payload: LLMSettingsIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if payload.model_priority_list is not None:
        if len(payload.model_priority_list) > settings_service.MAX_PRIORITY_LIST_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"model_priority_list supports at most {settings_service.MAX_PRIORITY_LIST_LEN} models",
            )
        if any(not isinstance(x, str) or not x.strip() for x in payload.model_priority_list):
            raise HTTPException(status_code=400, detail="model_priority_list must contain non-empty strings")
        if any(len(x) > MAX_MODEL_ITEM_LEN for x in payload.model_priority_list):
            raise HTTPException(
                status_code=400,
                detail=f"each model name must be at most {MAX_MODEL_ITEM_LEN} characters",
            )

    base_url = await _normalize_base_url(payload.base_url)
    api_key = _normalize_api_key(payload.api_key)
    model = _normalize_model(payload.model)
    vision_model = _normalize_model(payload.vision_model)
    vision_base_url = await _normalize_base_url(payload.vision_base_url)
    vision_api_key = _normalize_api_key(payload.vision_api_key)

    # 手机端会同时跑主动消息/说说/生活状态几个定时任务，SQLite 偶尔会被另一个
    # 线程占着写锁。以前这里只捕 OperationalError，锁冲突以 StatementError 冒
    # 出来就变成 500；现在整段（含写完后的回读）都兜住，并且等一会儿重试整笔
    # 事务 —— 用户点一下保存不该看到内部错误。
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            await settings_service.set_llm_config(
                session, base_url=base_url, api_key=api_key, model=model,
                vision_model=vision_model, vision_base_url=vision_base_url,
                vision_api_key=vision_api_key,
            )
            if payload.model_priority_list is not None:
                await settings_service.set_model_priority_list(session, payload.model_priority_list)
            await session.commit()

            settings_service.invalidate_cache()
            cfg = await settings_service.get_llm_config(session)
            return {
                "ok": True,
                "api_key_masked": settings_service.mask_key(cfg["api_key"]),
                "model_priority_list": await settings_service.get_model_priority_list(session),
            }
        except Exception as exc:  # noqa: BLE001 — 任何写入失败都必须降级为可重试的 503
            last_error = exc
            logger.exception("settings save failed (attempt %d/3)", attempt + 1)
            try:
                await session.rollback()
            except Exception:  # 会话已失效时 rollback 本身也会炸，不能让它再冒出去
                logger.exception("settings save: rollback failed")
            if attempt < 2:
                await asyncio.sleep(0.4 * (attempt + 1))

    raise HTTPException(
        status_code=503,
        detail=f"数据库写入失败（{_db_error_brief(last_error)}），请稍后重试",
        headers={"Retry-After": "2"},
    ) from last_error


@router.post("/test")
async def test_settings(session: AsyncSession = Depends(get_session)) -> dict:
    cfg = await settings_service.get_llm_config(session)
    if not cfg["api_key"]:
        return {"ok": False, "error": "未配置 API Key", "model_used": None, "attempts": []}

    priority = await settings_service.get_model_priority_list(session)
    model_list = [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]

    attempts: list[dict] = []
    try:
        _, model_used, attempts = await chat_completion_with_fallback_detail(
            [{"role": "user", "content": "ping"}],
            model_list,
            temperature=0.0,
            max_tokens=5,
        )
    except Exception as exc:  # clean error, never a traceback, never the key
        failed = getattr(exc, "attempts", attempts) or attempts
        return {
            "ok": False,
            "error": _sanitize_error(str(exc), cfg["api_key"]),
            "model_used": None,
            "attempts": _sanitize_attempts(failed, cfg["api_key"]),
        }

    return {
        "ok": True,
        "model_used": model_used,
        "attempts": _sanitize_attempts(attempts, cfg["api_key"]),
        "error": None,
    }


class AvatarUploadIn(BaseModel):
    image: str


@router.post("/avatar")
async def upload_avatar_alias(
    body: AvatarUploadIn,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """头像上传的别名端点，复用 companion 的实现。

    注意：不写 app/static —— Android 包内 static 是只读的。头像实际落在
    可写数据目录 `_DATA_DIR/avatars/`，再由 /api/companion/avatar/{fname} 提供。
    """
    from app.routers.companion import upload_avatar

    return await upload_avatar(body, user_id, session)
