from __future__ import annotations

import os
from pathlib import Path


# BASE_DIR: where the Python code lives inside the APK.
# On Android, Chaquopy bundles source to a read-only zip on the runtime classpath,
# but Path(__file__).resolve() still resolves correctly at runtime.
BASE_DIR = Path(__file__).resolve().parent.parent

# ANDROID_DATA_DIR is set by MainActivity.kt via os.environ before uvicorn.start().
# It's the App's private filesDir — writable, survives process restarts.
_ANDROID_DATA = os.environ.get("ANDROID_DATA_DIR", "")
if _ANDROID_DATA:
    _DATA_DIR = Path(_ANDROID_DATA)
else:
    _DATA_DIR = BASE_DIR

# .env on Android must live in the writable data dir (APK internal is read-only).
_env_candidates = [_DATA_DIR / ".env", BASE_DIR / ".env"]
_env_file = next((p for p in _env_candidates if p.exists()), _env_candidates[0])


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes).

    Replaces pydantic-settings so the Android build avoids pydantic v2
    (whose pydantic-core dependency is Rust-based and cannot be
    cross-compiled for Android without a Rust toolchain).
    """
    env: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key.strip()] = value
    except OSError:
        pass
    return env


def _coerce_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _coerce_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _coerce_tuple(value: str | None, default: tuple) -> tuple:
    if not value:
        return default
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    return tuple(parts) if parts else default


class Settings:
    """Plain settings object — env vars take priority over the .env file.

    Attribute names are identical to the previous pydantic-settings
    implementation, so no other module needed to change.
    """

    def __init__(self) -> None:
        env = _load_env_file(_env_file)
        merged = {**env, **{k: v for k, v in os.environ.items() if v != ""}}

        def get(key: str, default: str = "") -> str:
            return merged.get(key, default)

        # --- Database ---
        self.DATABASE_URL: str = get(
            "DATABASE_URL", f"sqlite+aiosqlite:///{_DATA_DIR / 'ai_friend.db'}"
        )

        # --- LLM ---
        self.LLM_BASE_URL: str = get("LLM_BASE_URL", "https://api.deepseek.com/v1")
        self.LLM_API_KEY: str = get("LLM_API_KEY", "")
        self.LLM_MODEL: str = get("LLM_MODEL", "deepseek-chat")
        # 视觉模型：多模态消息先用它把画面转成文字，再交给主模型。
        # 复用同一 BaseURL / Key，仅在单次请求里临时切换 model 名。
        self.VISION_MODEL: str = get("VISION_MODEL", "qwen-vl-plus")

        # --- Companion ---
        self.COMPANION_NAME: str = get("COMPANION_NAME", "小禾")

        # --- Safety ---
        self.CRISIS_KEYWORDS: tuple = _coerce_tuple(
            get("CRISIS_KEYWORDS"),
            ("自杀", "轻生", "想死", "不想活", "结束生命", "self-harm", "suicide", "kill myself"),
        )
        self.DEPENDENCY_KEYWORDS: tuple = _coerce_tuple(
            get("DEPENDENCY_KEYWORDS"),
            ("只有你", "离不开你", "没你不行", "你是我唯一"),
        )

        # --- Proactive ---
        self.PROACTIVE_ENABLED: bool = _coerce_bool(get("PROACTIVE_ENABLED"), True)
        self.MORNING_HOUR: int = _coerce_int(get("MORNING_HOUR"), 8)
        self.EVENING_HOUR: int = _coerce_int(get("EVENING_HOUR"), 22)

        # --- Auth ---
        self.AUTH_TOKEN: str = get("AUTH_TOKEN", "")

        # --- App ---
        self.DEBUG: bool = _coerce_bool(get("DEBUG"), False)
        self.SQL_ECHO: bool = _coerce_bool(get("SQL_ECHO"), False)
        self.HOST: str = get("HOST", "127.0.0.1")
        self.PORT: int = _coerce_int(get("PORT"), 8000)

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


settings = Settings()
