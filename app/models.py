from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> dt.datetime:
    """Naive UTC now — matches the naive DateTime columns (datetime.utcnow is deprecated)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# User + companion persona state
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="小友")
    companion_name: Mapped[str] = mapped_column(String(32), default="小禾")
    avatar_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    # Emotional state — mutable kernel (Layer B "what she says" + scalar state)
    # Layer A (appraisal) is stored as a compact JSON blob because its 5 Scherer
    # dims + 3 PAD + 3 momentum values are tightly coupled and updated every turn.
    appraisal_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    formulation_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Legacy scalar columns kept for dashboard + backward compat — derived from formulation.
    mood: Mapped[str] = mapped_column(String(16), default="calm")  # derived from formulation
    energy: Mapped[int] = mapped_column(Integer, default=70)
    intimacy: Mapped[int] = mapped_column(Integer, default=30)
    last_interaction_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    session_turns: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship stage
    relation_stage: Mapped[str] = mapped_column(String(16), default="exploratory")

    # CTEM feedback tracking
    neutral_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_valence: Mapped[float] = mapped_column(Float, default=0.0)
    last_message_len: Mapped[int] = mapped_column(Integer, default=0)
    challenge_counter: Mapped[int] = mapped_column(Integer, default=0)
    total_turns: Mapped[int] = mapped_column(Integer, default=0)

    # Proactive opt-ins
    morning_checkin: Mapped[bool] = mapped_column(Boolean, default=False)
    evening_checkin: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)

    persona_state: Mapped[Optional["PersonaState"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class PersonaState(Base):
    """Layer A — slow-drifting numeric trait vector + fixed persona recipe.

    recipe is 15 微性格维度，按 user_id 种子生成一次后永久固定，像真人的底层性格一样。
    warmth/playfulness/directness/protectiveness 是更粗粒度的 slow-drift 向量。
    """

    __tablename__ = "persona_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    warmth: Mapped[float] = mapped_column(Float, default=0.5)
    playfulness: Mapped[float] = mapped_column(Float, default=0.5)
    directness: Mapped[float] = mapped_column(Float, default=0.5)
    protectiveness: Mapped[float] = mapped_column(Float, default=0.5)
    # JSON blob: fixed 15-dim persona recipe generated once per user_id seed.
    # E.g. {"温柔": 72, "傲娇": 38, "怼人": 14, ...} — values 0–100.
    recipe: Mapped[Optional[str]] = mapped_column(Text, default=None)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Virtual life state — what she's currently "doing".
    current_activity: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    activity_updated_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)

    # She has her own energy — independent from the user's energy model.
    # Drains when she outputs long replies; refills with quiet time.
    # Range 0–100; <30 means she's genuinely tired and should say so.
    self_energy: Mapped[float] = mapped_column(Float, default=70.0)
    energy_updated_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="persona_state")


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(8))  # "user" / "assistant" / "system"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # True when she spoke up on her own (scheduler-driven, no user prompt).
    proactive: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, nullable=True)


# ---------------------------------------------------------------------------
# Memory (four layers — all user-visible, soft-deletable)
# ---------------------------------------------------------------------------

class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    # Layer discriminator
    layer: Mapped[str] = mapped_column(String(16))  # episodic / semantic / pinned / ephemeral

    content: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)

    # Emotional tag (nullable), intensity 1–5
    emotion: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    intensity: Mapped[int] = mapped_column(Integer, default=3)

    # Semantic facts carry a structured value + confidence
    fact_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    fact_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)

    # Salience decays; pinned never does
    salience: Mapped[float] = mapped_column(Float, default=1.0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_referenced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "layer", "fact_key", name="uq_user_layer_fact"),)


# ---------------------------------------------------------------------------
# Layer B — self reflections
# ---------------------------------------------------------------------------

class SelfReflection(Base):
    __tablename__ = "self_reflections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_turn_count: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Scheduled proactive check-ins
# ---------------------------------------------------------------------------

class ProactiveLog(Base):
    __tablename__ = "proactive_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # morning / evening
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    gate_pass: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# In-app settings (LLM overrides; .env remains the fallback)
# ---------------------------------------------------------------------------

class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# QQ Zone / Moments — 她的说说
# ---------------------------------------------------------------------------

class Moment(Base):
    __tablename__ = "moments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    is_liked_by_user: Mapped[bool] = mapped_column(Boolean, default=False)


class MomentComment(Base):
    __tablename__ = "moment_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    moment_id: Mapped[int] = mapped_column(ForeignKey("moments.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # "user" / "companion"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ensure_user(session, user_id: int = 1) -> User:
    """Return the singleton demo user, creating + initialising persona_state if needed."""
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(User).options(selectinload(User.persona_state)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(id=user_id)
        session.add(user)
        await session.flush()
    if user.persona_state is None:
        user.persona_state = PersonaState(user_id=user.id)
        session.add(user.persona_state)
    return user
