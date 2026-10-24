"""
In-app LLM configuration: DB overrides with .env fallback.

Precedence: a non-empty ``app_settings`` row wins; otherwise fall back to
``app.config.settings``. An empty string clears the override.

A 5-second in-process cache keeps ``llm_service`` off the DB for every call.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import settings

PRIORITY_KEY = "model_priority_list"
VISION_KEY = "vision_model"
VISION_BASE_URL_KEY = "vision_base_url"
VISION_API_KEY_KEY = "vision_api_key"
_KEYS = ("llm_base_url", "llm_api_key", "llm_model", PRIORITY_KEY, VISION_KEY,
         VISION_BASE_URL_KEY, VISION_API_KEY_KEY)
_CACHE_TTL_SECONDS = 5.0

# Tried after the configured model, in order, on retryable failures.
# All on DeepSeek — 0 prefix tokens cached at 0.02 RMB/million, 50% off peak.
DEFAULT_MODEL_PRIORITY_LIST = [
    "deepseek-chat",        # smarter full model — primary for quality
    "deepseek-chat",        # try again on same model before falling back (transient 429s)
    "deepseek-chat",        # third retry — DeepSeek is very stable
]
MAX_PRIORITY_LIST_LEN = 15

_cache: Optional[dict] = None
_cache_at: float = 0.0


def _parse_priority(raw: Optional[str]) -> list[str]:
    if not raw:
        return list(DEFAULT_MODEL_PRIORITY_LIST)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return list(DEFAULT_MODEL_PRIORITY_LIST)
    if (
        not isinstance(value, list)
        or len(value) > MAX_PRIORITY_LIST_LEN
        or not all(isinstance(x, str) and x.strip() for x in value)
    ):
        return list(DEFAULT_MODEL_PRIORITY_LIST)
    return [x.strip() for x in value]


def invalidate_cache() -> None:
    """Drop the cached config (called after writes)."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def mask_key(k: str) -> str:
    """Return a display-safe key. Never returns the full value."""
    if not k:
        return ""
    # Short keys would lose too much entropy to a first7/last4 mask.
    if len(k) < 20:
        return "***"
    return f"{k[:7]}...{k[-4:]}"


async def _load_config(session: AsyncSession) -> dict:
    rows = (
        await session.execute(select(m.AppSetting).where(m.AppSetting.key.in_(_KEYS)))
    ).scalars().all()
    stored = {row.key: (row.value or "") for row in rows}

    cfg = {
        "base_url": stored.get("llm_base_url") or settings.LLM_BASE_URL,
        "api_key": stored.get("llm_api_key") or settings.LLM_API_KEY,
        "model": stored.get("llm_model") or settings.LLM_MODEL,
        "model_priority_list": _parse_priority(stored.get(PRIORITY_KEY)),
        # 视觉模型：留空时由调用方回退到环境变量/主模型
        "vision_model": stored.get(VISION_KEY, ""),
        # 视觉可独立走另一家提供商（如 GLM-4V）；留空则复用主配置的 base_url/key
        "vision_base_url": stored.get(VISION_BASE_URL_KEY, ""),
        "vision_api_key": stored.get(VISION_API_KEY_KEY, ""),
    }

    global _cache, _cache_at
    _cache = dict(cfg)
    _cache_at = time.monotonic()
    return cfg


async def get_llm_config(session: Optional[AsyncSession] = None) -> dict:
    """Return effective ``{base_url, api_key, model}``.

    ``session`` may be omitted; then a short-lived session is opened only on a
    cache miss, so the hot path usually performs no DB work.
    """
    if _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL_SECONDS:
        cfg = _cache
    elif session is not None:
        cfg = await _load_config(session)
    else:
        from app.database import async_session

        async with async_session() as own_session:
            cfg = await _load_config(own_session)

    return {key: cfg[key] for key in ("base_url", "api_key", "model", "vision_model",
                                      "vision_base_url", "vision_api_key")}


async def get_model_priority_list(session: Optional[AsyncSession] = None) -> list[str]:
    """Return the failover model list (default when no override is stored)."""
    if _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL_SECONDS:
        return list(_cache["model_priority_list"])

    if session is not None:
        cfg = await _load_config(session)
    else:
        from app.database import async_session

        async with async_session() as own_session:
            cfg = await _load_config(own_session)

    return list(cfg["model_priority_list"])


async def _write_setting(session: AsyncSession, key: str, value: str) -> None:
    """Delete-then-insert inside the caller's transaction.

    Two mobile-only landmines ruled out here:
      * ``ON CONFLICT DO UPDATE`` needs SQLite >= 3.24 *and* a UNIQUE index
        whose shape matches the clause; Android ships whatever SQLite the
        device has, and a DB created by an older build may lack that index.
      * SELECT-then-INSERT races two concurrent saves into UNIQUE(key).

    DELETE + INSERT is plain SQL that works on every SQLite version, and the
    delete takes the write lock first so a concurrent save simply queues
    behind it instead of failing on the constraint.
    """
    await session.execute(delete(m.AppSetting).where(m.AppSetting.key == key))
    await session.execute(
        m.AppSetting.__table__.insert().values(
            key=key, value=value, updated_at=m.utcnow(),
        )
    )


async def set_model_priority_list(session: AsyncSession, models: list[str]) -> None:
    """Store the priority list as JSON. An empty list clears the override."""
    # Dedupe while preserving order — a pasted "a, a, b" collapses to ["a", "b"].
    cleaned: list[str] = []
    for x in models:
        if not isinstance(x, str):
            continue
        x = x.strip()
        if x and x not in cleaned:
            cleaned.append(x)
    cleaned = cleaned[:MAX_PRIORITY_LIST_LEN]

    if not cleaned:
        await session.execute(delete(m.AppSetting).where(m.AppSetting.key == PRIORITY_KEY))
    else:
        payload = json.dumps(cleaned, ensure_ascii=False)
        await _write_setting(session, PRIORITY_KEY, payload)

    await session.flush()
    invalidate_cache()


async def set_llm_config(
    session: AsyncSession,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    vision_model: Optional[str] = None,
    vision_base_url: Optional[str] = None,
    vision_api_key: Optional[str] = None,
) -> None:
    """Upsert only the provided fields. Empty string clears an override.

    The API key value is never logged.
    """
    updates = {
        "llm_base_url": base_url,
        "llm_api_key": api_key,
        "llm_model": model,
        VISION_KEY: vision_model,
        VISION_BASE_URL_KEY: vision_base_url,
        VISION_API_KEY_KEY: vision_api_key,
    }

    for key, value in updates.items():
        if value is None:
            continue
        if value == "":
            await session.execute(delete(m.AppSetting).where(m.AppSetting.key == key))
            continue

        await _write_setting(session, key, value)

    await session.flush()
    invalidate_cache()
