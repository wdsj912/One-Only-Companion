from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent

# On Android (Chaquopy) the APK bundle is read-only — writable state (.env,
# SQLite, .auth_token) lives in the app's private filesDir, exposed via the
# ANDROID_DATA_DIR env var set by MainActivity before uvicorn starts.
_ANDROID_DATA = os.environ.get("ANDROID_DATA_DIR", "")
_DATA_DIR = Path(_ANDROID_DATA) if _ANDROID_DATA else BASE_DIR
_env_candidates = [_DATA_DIR / ".env", BASE_DIR / ".env"]
_env_file = next((p for p in _env_candidates if p.exists()), _env_candidates[0])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_env_file), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database ---
    DATABASE_URL: str = f"sqlite+aiosqlite:///{_DATA_DIR / 'ai_friend.db'}"

    # --- LLM ---
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    # 视觉模型：多模态消息先用它把画面转成文字，再交给主模型。
    # 复用同一 BaseURL / Key，仅在单次请求里临时切换 model 名。
    VISION_MODEL: str = "qwen-vl-plus"

    # --- Companion ---
    COMPANION_NAME: str = "小禾"

    # --- Safety ---
    # Keywords that trigger the crisis response (simple substring match).
    CRISIS_KEYWORDS: tuple[str, ...] = (
        "自杀", "轻生", "想死", "不想活", "结束生命", "self-harm", "suicide", "kill myself",
    )
    DEPENDENCY_KEYWORDS: tuple[str, ...] = (
        "只有你", "离不开你", "没你不行", "你是我唯一",
    )

    # --- Proactive ---
    PROACTIVE_ENABLED: bool = True
    MORNING_HOUR: int = 8
    EVENING_HOUR: int = 22

    # --- Auth ---
    # Access token for every /api route. Leave empty to let the app generate one
    # and persist it to .auth_token (the token itself is never logged).
    AUTH_TOKEN: str = ""

    # --- App ---
    DEBUG: bool = False
    # SQL echo is opt-in only: it prints statement parameters verbatim, which
    # would leak API keys stored in app_settings.
    SQL_ECHO: bool = False
    # Bind to loopback by default: the app is single-user and only protected by
    # the access token, so LAN exposure should be an explicit opt-in via .env.
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


settings = Settings()
