"""
CTEM closed loop — post-turn, async, never on the hot path.

  past episode
    → update emotional state (Layer A and B via emotion_kernel.transition)
    → state conditions next interaction via prompt injection
    → post-turn user feedback (engagement length delta, sentiment shift, follow-up rate, topic abandonment)
    → revise BOTH memory salience AND emotional state.

Deterministic, zero-LLM.  If feedback signal is absent or neutral for 3
consecutive turns, treat as mild negative evidence and slightly reduce
formulation valence + warmth.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.services.emotion_kernel import EmotionState


# ---------------------------------------------------------------------------
# Feedback signal extraction — purely deterministic.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeedbackSignal:
    length_delta: float          # change in avg message length vs rolling baseline
    sentiment_shift: float       # [-1, 1] from the last kernel valence transition
    follow_up: bool              # did user ask a follow-up within the same turn?
    topic_abandoned: bool        # last user message switched topic without warning
    is_neutral: bool             # all signals ≈ 0 / default → treat as absence


def extract_feedback(
    prev_valence: float,
    current_valence: float,
    prev_len: int,
    current_len: int,
    user_asked_followup: bool,
    topic_switch: bool,
) -> FeedbackSignal:
    length_delta = (current_len - prev_len) / max(1, prev_len)
    sentiment_shift = current_valence - prev_valence
    # "Neutral" → very small absolute values on every axis AND no follow-up / no topic
    is_neutral = (
        abs(length_delta) < 0.15
        and abs(sentiment_shift) < 0.15
        and not user_asked_followup
        and not topic_switch
    )
    return FeedbackSignal(
        length_delta=length_delta,
        sentiment_shift=sentiment_shift,
        follow_up=user_asked_followup,
        topic_abandoned=topic_switch,
        is_neutral=is_neutral,
    )


# ---------------------------------------------------------------------------
# Feedback application — revises BOTH memory salience AND emotional state.
# Pure function on EmotionState; memory revision needs DB session.
# ---------------------------------------------------------------------------

def apply_feedback_to_state(state: EmotionState, signal: FeedbackSignal, neutral_streak: int) -> EmotionState:
    """Revise formulation (Layer B) based on user feedback.

    Rules:
      * Positive signal (follow-up, length up, sentiment up) → slightly warm
        formulation valence (+0.03).
      * Negative (topic abandoned, sentiment dropped, length dropped a lot) →
        cool formulation valence (-0.05) and slightly raise suppression gate.
      * 3 consecutive neutrals → mild negative: formulation valence -0.03,
        suppression +0.05.  NOT a crisis — just low-level drift.
    """
    f = state.formulation
    new_v = f.valence
    new_a = f.arousal
    new_sup = f.suppression_gate

    if signal.is_neutral:
        if neutral_streak >= 2:  # 3rd consecutive
            new_v = max(-1.0, new_v - 0.03)
            new_sup = min(0.8, new_sup + 0.05)
    else:
        if signal.follow_up or signal.sentiment_shift > 0.2:
            new_v = min(1.0, new_v + 0.03)
        if signal.topic_abandoned or signal.length_delta < -0.25:
            new_v = max(-1.0, new_v - 0.05)
            new_sup = min(0.8, new_sup + 0.08)
        if signal.sentiment_shift < -0.2:
            new_a = max(0.0, new_a - 0.05)

    from app.services.emotion_kernel import Formulation
    new_formulation = Formulation(
        valence=max(-1.0, min(1.0, new_v)),
        arousal=max(0.0, min(1.0, new_a)),
        dominance=max(-1.0, min(1.0, f.dominance)),
        suppression_gate=max(0.0, min(0.8, new_sup)),
    )
    return EmotionState(
        appraisal=state.appraisal,
        formulation=new_formulation,
        intimacy=state.intimacy,
        energy=state.energy,
        last_interaction_at=state.last_interaction_at,
        turns_since_arousal_decay=state.turns_since_arousal_decay,
        session_turns=state.session_turns,
    )


async def apply_feedback_to_memory_salience(
    session: Session,
    user_id: int,
    *,
    reward: bool = False,
    penalty: bool = False,
    neutral_streak: int = 0,
) -> None:
    """Revise salience on retrieved memories.

    * Positive feedback → bump the last-retrieved memories' salience +0.05.
    * Negative feedback → decay all salience by -0.03 (except pinned).
    * Long neutral streak → gentle global decay (-0.01) so unanchored
      memories fade a little faster when the user isn't engaging.
    """
    stmt = select(m.Memory).where(
        m.Memory.user_id == user_id,
        m.Memory.deleted_at.is_(None),
    )
    all_mems = list((await session.execute(stmt)).scalars().all())

    for mem in all_mems:
        if mem.pinned:
            continue
        if reward:
            if mem.last_referenced_at is not None:
                mem.salience = min(1.0, (mem.salience or 0.5) + 0.05)
        elif penalty:
            mem.salience = max(0.0, (mem.salience or 0.5) - 0.03)
        elif neutral_streak >= 3:
            mem.salience = max(0.0, (mem.salience or 0.5) - 0.01)


# ---------------------------------------------------------------------------
# RelationStage transition — driven by intimacy + session count.
# Cumulative, NEVER by a single good conversation.
# ---------------------------------------------------------------------------

from app.services.emotion_kernel import RelationStage as _RS

# (stage, 累计用户消息数阈值) —— 8 档，7 个台阶
_STAGE_THRESHOLDS: tuple[tuple[_RS, int], ...] = (
    (_RS.ACQUAINTED, 100),
    (_RS.FAMILIAR, 300),
    (_RS.CLOSE, 600),
    (_RS.AFFECTIVE, 1000),
    (_RS.FLIRTING, 1500),
    (_RS.INTIMATE, 2100),
    (_RS.DEEPENING, 2800),
)


def transition_relation_stage(current: _RS, total_user_turns: int) -> _RS:
    """八档关系刻度 —— 只看累计对话轮数，情绪再浓也不会跳档。

        1 陌生      0
        2 认识      100 轮
        3 熟络      300
        4 亲近      600
        5 有好感   1000
        6 暧昧     1500
        7 亲密     2100
        8 羁绊     2800

    阈值是三角数 ×100（间隔 200/300/400…），越往后越难升。
    关系只会向前，不会回落 —— 信任是粘的。
    """
    from app.services.emotion_kernel import stage_index

    current_idx = stage_index(current)
    target = current
    for idx, (stage, threshold) in enumerate(_STAGE_THRESHOLDS, start=1):
        if total_user_turns >= threshold and idx > current_idx:
            target = stage
    return target
