"""Single-user token authentication for every ``/api`` route.

Token source, in order:

1. ``AUTH_TOKEN`` in ``.env``;
2. ``.auth_token`` in the project root (written on first start);
3. a freshly generated token, persisted to ``.auth_token``.

The token is never logged — only its file path is. Browsers get it as an
HttpOnly ``SameSite=Strict`` cookie via ``POST /api/auth/login``; scripts can
send it as the ``X-API-Token`` header.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Header, HTTPException

from app.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "ai_friend_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
# On Android (Chaquopy) the bundle dir is read-only — the token must live in the
# app's private filesDir, exposed via ANDROID_DATA_DIR before uvicorn starts.
_ANDROID_DATA = os.environ.get("ANDROID_DATA_DIR", "")
_TOKEN_DIR = Path(_ANDROID_DATA) if _ANDROID_DATA else BASE_DIR
TOKEN_FILE = _TOKEN_DIR / ".auth_token"

_token: Optional[str] = None


def get_auth_token() -> str:
    """Return the access token, generating and persisting one on first use."""
    global _token
    if _token:
        return _token

    configured = (settings.AUTH_TOKEN or "").strip()
    if configured:
        _token = configured
        return _token

    if TOKEN_FILE.exists():
        stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if stored:
            _token = stored
            return _token

    generated = secrets.token_urlsafe(32)
    try:
        TOKEN_FILE.write_text(generated, encoding="utf-8")
        logger.warning("已生成访问令牌，保存在 %s（内容不会写入日志）", TOKEN_FILE)
    except OSError as exc:
        logger.warning("访问令牌文件写入失败（%s），本次运行使用临时令牌", exc)
    _token = generated
    return _token


def token_matches(candidate: Optional[str]) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate, get_auth_token())


async def require_auth(
    x_api_token: Optional[str] = Header(default=None),
    session_cookie: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> None:
    """FastAPI dependency: 401 unless the header or cookie carries the token."""
    if token_matches(x_api_token) or token_matches(session_cookie):
        return
    raise HTTPException(
        status_code=401,
        detail="unauthorized",
        headers={"WWW-Authenticate": "X-API-Token"},
    )
