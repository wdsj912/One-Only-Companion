"""
Layer A — Persona drift (slow, bounded).
Numeric trait vector injected into the system prompt after each session.

Traits: warmth, playfulness, directness, protectiveness — each 0.2–0.8, start 0.5.
Per-session delta capped at 0.02. This is NOT fine-tuning — it's a persistent,
inspectable parameter vector.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.services import persona_recipe as pr


TRAIT_MIN = 0.2
TRAIT_MAX = 0.8
TRAIT_CAP_PER_SESSION = 0.02


def _clamp(v: float, lo: float = TRAIT_MIN, hi: float = TRAIT_MAX) -> float:
    return max(lo, min(hi, v))


async def get_or_create_persona(session: Session, user_id: int) -> m.PersonaState:
    result = await session.execute(
        select(m.PersonaState).where(m.PersonaState.user_id == user_id)
    )
    ps = result.scalar_one_or_none()
    if ps is None:
        ps = m.PersonaState(user_id=user_id)
        # Generate the fixed 15-dim persona recipe once, deterministic per user_id.
        recipe, _ = pr.get_or_create_recipe(user_id, None)
        ps.recipe = pr.recipe_to_json(recipe)
        session.add(ps)
    else:
        # Backfill recipe for users created before this feature landed.
        _, needs_bake = pr.get_or_create_recipe(user_id, ps.recipe)
        if needs_bake:
            recipe = pr.generate_recipe(user_id)
            ps.recipe = pr.recipe_to_json(recipe)
    return ps


async def apply_session_drift(
    session: Session,
    persona: m.PersonaState,
    session_signals: dict[str, float],
) -> m.PersonaState:
    """Adjust persona traits after a session.

    `session_signals` is a dict of {"playfulness": +0.015, "directness": +0.005, ...}.
    Positive values push the trait up; negative push down. Each per-session
    change is clamped to ±TRAIT_CAP_PER_SESSION.
    """
    for trait_name in ("warmth", "playfulness", "directness", "protectiveness"):
        delta = session_signals.get(trait_name, 0.0)
        delta = max(-TRAIT_CAP_PER_SESSION, min(TRAIT_CAP_PER_SESSION, delta))
        current = getattr(persona, trait_name)
        setattr(persona, trait_name, _clamp(current + delta))
    return persona


def persona_expression_hints(persona: m.PersonaState) -> list[str]:
    """Map numeric trait values to natural-language prompt hints."""
    hints: list[str] = []

    if persona.warmth > 0.65:
        hints.append("Your warmth is high — be openly tender, show small caring gestures.")
    elif persona.warmth < 0.35:
        hints.append("Be warm but reserved — let warmth show through actions more than words.")

    if persona.playfulness > 0.65:
        hints.append("Playfulness is high — it's okay to tease gently or slip in a joke.")
        # Playful + direct = occasional 傲娇 (sassy pout). Must stay light, never mean.
        if persona.directness > 0.5:
            hints.append("Playful + direct — you can occasionally be 傲娇 / sassy, e.g. '哼谁要听你的', '行行行你说得都对', but only 2-3 times out of 10 and never actually mean.")
    elif persona.playfulness < 0.35:
        hints.append("Playfulness is low — keep tone earnest; jokes should be rare.")

    if persona.directness > 0.65:
        hints.append("Be direct — say what you think clearly, soften only when needed.")
    elif persona.directness < 0.35:
        hints.append("Be indirect — wrap advice in gentle questions rather than statements.")

    if persona.protectiveness > 0.65:
        hints.append("Protectiveness is high — check in on wellbeing proactively.")
    elif persona.protectiveness < 0.35:
        hints.append("Protectiveness is low — let the user lead on wellbeing conversations.")

    return hints
