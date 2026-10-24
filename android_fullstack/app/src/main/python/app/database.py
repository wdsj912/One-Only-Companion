from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import text

from app.config import settings

logger = logging.getLogger(__name__)


class Base(AsyncAttrs, DeclarativeBase):
    pass


def create_engine() -> AsyncEngine:
    connect_args = {}
    if settings.is_sqlite:
        connect_args["check_same_thread"] = False
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.SQL_ECHO,
        connect_args=connect_args,
        future=True,
    )
    # SQLite WAL + busy_timeout — must run on every new connection so it
    # survives process restarts and covers both sync and async callers.
    if settings.is_sqlite:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = create_engine()
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create all tables on startup (for dev). Production would use Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Best-effort schema migration for SQLite — ALTER TABLE ADD COLUMN.
    # Each migration is isolated: a transient lock or readonly FS on Android
    # must never abort the whole lifespan (which would 500 every endpoint).
    if settings.is_sqlite:
        for _migration in (
            _ensure_message_proactive_column,
            _ensure_persona_recipe_column,
            _ensure_persona_activity_columns,
            _ensure_persona_energy_columns,
            _ensure_moment_image_column,
            _ensure_user_avatar_column,
        ):
            try:
                await _migration()
            except Exception as exc:
                logger.warning("migration %s skipped: %s", _migration.__name__, exc)


async def _ensure_user_avatar_column() -> None:
    """Add avatar_url to users if missing (avatar upload feature)."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(users)"))
        cols = {row[1] for row in result}
        if "avatar_url" not in cols:
            await conn.execute(
                text("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(255)")
            )


async def _ensure_persona_recipe_column() -> None:
    """Add the recipe (JSON text blob) column to persona_state if missing."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(persona_state)"))
        cols = {row[1] for row in result}
        if "recipe" not in cols:
            await conn.execute(
                text("ALTER TABLE persona_state ADD COLUMN recipe TEXT DEFAULT NULL")
            )


async def _ensure_message_proactive_column() -> None:
    """Add the proactive column to messages if it doesn't already exist."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(messages)"))
        cols = {row[1] for row in result}
        if "proactive" not in cols:
            await conn.execute(
                text("ALTER TABLE messages ADD COLUMN proactive BOOLEAN DEFAULT 0")
            )


async def _ensure_persona_activity_columns() -> None:
    """Add current_activity + activity_updated_at to persona_state if missing."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(persona_state)"))
        cols = {row[1] for row in result}
        if "current_activity" not in cols:
            await conn.execute(
                text("ALTER TABLE persona_state ADD COLUMN current_activity TEXT")
            )
        if "activity_updated_at" not in cols:
            await conn.execute(
                text("ALTER TABLE persona_state ADD COLUMN activity_updated_at DATETIME")
            )


async def _ensure_persona_energy_columns() -> None:
    """Add self_energy + energy_updated_at to persona_state if missing."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(persona_state)"))
        cols = {row[1] for row in result}
        if "self_energy" not in cols:
            await conn.execute(
                text("ALTER TABLE persona_state ADD COLUMN self_energy FLOAT DEFAULT 70.0")
            )
        if "energy_updated_at" not in cols:
            await conn.execute(
                text("ALTER TABLE persona_state ADD COLUMN energy_updated_at DATETIME")
            )


async def _ensure_moment_image_column() -> None:
    """Add the image column (data URL / base64) to moments if missing."""
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(moments)"))
            cols = {row[1] for row in result}
            if "image" not in cols:
                await conn.execute(text("ALTER TABLE moments ADD COLUMN image TEXT"))
    except Exception as exc:  # never block startup over a migration
        logger.debug("moments image column migration skipped: %s", exc)


async def get_session():
    """FastAPI dependency for request-scoped sessions.

    收尾阶段的 commit/rollback 抛错不能让"已经成功的请求"变成 500 ——
    手机端这种错误就是被包成 服务器内部错误（StatementError）弹到用户脸上的。
    业务路由自己会显式 commit，这里只做兜底与记录。
    """
    async with async_session() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:
                logger.exception("session rollback failed")
            raise
        else:
            try:
                await session.commit()
            except Exception:
                logger.exception("session commit on teardown failed")
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("session rollback failed")
