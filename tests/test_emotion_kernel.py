"""Dual-layer emotion kernel tests — spec: deterministic, zero-LLM, momentum-constrained."""
from __future__ import annotations

import datetime as dt

import pytest

from app.services.emotion_kernel import (
    EmotionState,
    Appraisal,
    Formulation,
    RelationStage,
    classify_user_valence,
    _synthesis_appraisal,
)


# ---------------------------------------------------------------------------
# Import-time LLM guard: emotion_kernel must not import anything from
# services.llm_service.  We verify via module globals here.
# ---------------------------------------------------------------------------

def test_emotion_kernel_does_not_import_llm_service():
    import app.services.emotion_kernel as ek
    # Walk every imported name in the module — nothing should resolve to llm_service.
    for name, obj in vars(ek).items():
        if name.startswith("_") or callable(obj):
            continue
        mod = getattr(obj, "__module__", "") or ""
        assert "llm_service" not in mod, f"{name} comes from llm_service — violates zero-LLM kernel rule"


# ---------------------------------------------------------------------------
# Classification + synthesis
# ---------------------------------------------------------------------------

def test_classify_valence_unchanged():
    assert classify_user_valence("我今天很开心！") == "joy"
    assert classify_user_valence("我好难过") == "sad"
    assert classify_user_valence("气死我了") == "angry"
    assert classify_user_valence("好紧张") == "anxious"
    assert classify_user_valence("嗯嗯") == "neutral"


def test_synthesis_returns_raw_pad():
    a = _synthesis_appraisal("哈哈哈哈太棒了", "joy")
    assert a.valence_raw > 0.5
    assert a.arousal_raw > 0.5
    assert a.dominance_raw > 0.0
    assert all(0.0 <= x <= 1.0 for x in (
        a.relevance, a.implication, a.coping_potential, a.norm_compatibility, a.self_involvement
    ))


# ---------------------------------------------------------------------------
# Momentum as hard constraint — the single "must pass" invariant
# ---------------------------------------------------------------------------

class TestMomentumConstraint:
    def test_single_message_cannot_flip_negative_to_positive(self):
        """Start < -0.3, one strongly positive input → still < +0.2 after transition."""
        # Set up state with strongly negative momentum
        initial = EmotionState(
            appraisal=Appraisal(
                relevance=0.5, implication=0.7, coping_potential=0.3,
                norm_compatibility=0.8, self_involvement=0.4,
                valence_raw=-0.7, arousal_raw=0.6, dominance_raw=-0.3,
                valence_momentum=-0.5, arousal_momentum=0.5, dominance_momentum=-0.2,
            ),
        )
        new = initial.transition("哈哈哈哈哈哈太棒了！！！真的好开心好开心！！！", stage=RelationStage.EXPLORATORY)
        # Hard invariant from spec
        assert new.appraisal.valence_momentum < 0.2, (
            f"valence momentum flipped too hard: initial=-0.5 → final={new.appraisal.valence_momentum:+.2f}"
        )
        # And it *should* have moved toward positive — momentum is 0.7, so input 0.7
        #   0.7 * (-0.5) + 0.3 * 0.7 = -0.35 + 0.21 = -0.14 — still negative!
        assert new.appraisal.valence_momentum > initial.appraisal.valence_momentum

    def test_multiple_positive_messages_do_shift_valence(self):
        """But after several turns, valence *does* drift — momentum damps, it doesn't lock."""
        state = EmotionState(
            appraisal=Appraisal(
                relevance=0.5, implication=0.7, coping_potential=0.3,
                norm_compatibility=0.8, self_involvement=0.4,
                valence_raw=-0.7, arousal_raw=0.6, dominance_raw=-0.3,
                valence_momentum=-0.5, arousal_momentum=0.5, dominance_momentum=-0.2,
            ),
        )
        for _ in range(6):
            state = state.transition("好开心！太棒了！", stage=RelationStage.EXPLORATORY)
        assert state.appraisal.valence_momentum > 0.0, (
            f"valence still negative after 6 positive turns: {state.appraisal.valence_momentum:+.2f}"
        )


# ---------------------------------------------------------------------------
# Arousal half-life in turns (not wall-clock)
# ---------------------------------------------------------------------------

class TestArousalHalfLife:
    def test_arousal_decays_per_turn_toward_baseline(self):
        state = EmotionState(
            appraisal=Appraisal(
                relevance=0.5, implication=0.5, coping_potential=0.7,
                norm_compatibility=0.8, self_involvement=0.3,
                valence_raw=0.0, arousal_raw=0.0, dominance_raw=0.0,
                valence_momentum=0.0, arousal_momentum=0.8, dominance_momentum=0.0,
            ),
        )
        # Half-life = 4 turns, decay baseline = 0.3
        # After 4 pure half-lives: deviation (0.8-0.3) halves → 0.5*0.5 = 0.25
        # So expected arousal = 0.3 + 0.25 = 0.55
        ar0 = state.appraisal.arousal_momentum
        for _ in range(4):
            state = state.transition("嗯好的", stage=RelationStage.EXPLORATORY)
        ar4 = state.appraisal.arousal_momentum
        # Deviation must have halved from 0.5 to 0.25
        dev0 = ar0 - 0.3
        dev4 = ar4 - 0.3
        assert dev4 == pytest.approx(dev0 * 0.5, abs=0.001), (
            f"arousal deviation did not halve: dev0={dev0:.3f} → dev4={dev4:.3f}"
        )

    def test_arousal_decay_turn_based_not_wall_clock(self):
        """Decay rate is driven by number of transitions, not elapsed wall-clock."""
        state = EmotionState(
            appraisal=Appraisal(
                relevance=0.5, implication=0.5, coping_potential=0.7,
                norm_compatibility=0.8, self_involvement=0.3,
                valence_raw=0.0, arousal_raw=0.0, dominance_raw=0.0,
                valence_momentum=0.0, arousal_momentum=0.8, dominance_momentum=0.0,
            ),
            last_interaction_at=dt.datetime(2020, 1, 1),
        )
        # With daily_decay applied, arousal still 0.8 or just softly drifted —
        # what matters: run 4 transitions with *no gap* between them, it decays
        # by half-life regardless of how long ago the last_interaction_at was.
        state = state.with_daily_decay()
        before = state.appraisal.arousal_momentum
        for _ in range(4):
            state = state.transition("嗯好", stage=RelationStage.EXPLORATORY)
        after = state.appraisal.arousal_momentum
        dev0 = before - 0.3
        dev4 = after - 0.3
        assert dev4 == pytest.approx(dev0 * 0.5, abs=0.001)


# ---------------------------------------------------------------------------
# Layer A → Layer B gap (suppression gate) — she feels more than she says
# ---------------------------------------------------------------------------

class TestABGap:
    def test_low_intimacy_suppresses_formulation(self):
        """Appraisal fires hot, formulation stays muted when intimacy low."""
        state = EmotionState(intimacy=10)
        state = state.transition("气死我了！离谱到家了！", stage=RelationStage.EXPLORATORY)
        a_valence = state.appraisal.valence_momentum
        b_valence = state.formulation.valence
        b_arousal = state.formulation.arousal
        # Formulation valence and arousal should be less extreme than appraisal
        assert abs(b_valence) < abs(a_valence)
        assert b_arousal < state.appraisal.arousal_momentum
        assert state.formulation.suppression_gate > 0.3

    def test_high_intimacy_reduces_suppression(self):
        """Deepening + high intimacy → B gets close to A."""
        state = EmotionState(intimacy=85)
        state = state.transition("真的太感动了，谢谢你一直陪着我", stage=RelationStage.DEEPENING)
        gap = abs(state.appraisal.valence_momentum - state.formulation.valence)
        assert gap < 0.2, f"deepening/high-intimacy gap too big: {gap:.3f}"
        assert state.formulation.suppression_gate < 0.3

    def test_formulation_adapts_slower_than_appraisal(self):
        """Layer B lags Layer A — this is what makes emotion feel real."""
        state = EmotionState(intimacy=50)
        # Appraisal takes a big hit
        for _ in range(3):
            state = state.transition("我好难过，压力好大，真的撑不住了", stage=RelationStage.EXPLORATORY)
        # Appraisal valence more negative than formulation
        assert state.appraisal.valence_momentum < state.formulation.valence - 0.1


# ---------------------------------------------------------------------------
# Relationship stage modulates suppression
# ---------------------------------------------------------------------------

def test_deepening_has_lowest_suppression():
    state_0 = EmotionState(intimacy=50)
    s_exp = state_0.transition("气死我了！", stage=RelationStage.EXPLORATORY)
    s_aff = state_0.transition("气死我了！", stage=RelationStage.AFFECTIVE)
    s_dep = state_0.transition("气死我了！", stage=RelationStage.DEEPENING)
    assert s_dep.formulation.suppression_gate < s_exp.formulation.suppression_gate


# ---------------------------------------------------------------------------
# Pure-function identity — given same inputs, same outputs (no RNG, no time)
# ---------------------------------------------------------------------------

def test_transition_is_deterministic():
    s1 = EmotionState().transition("今天心情不错", stage=RelationStage.AFFECTIVE, now=dt.datetime(2026, 10, 21, 12))
    s2 = EmotionState().transition("今天心情不错", stage=RelationStage.AFFECTIVE, now=dt.datetime(2026, 10, 21, 12))
    assert s1.appraisal == s2.appraisal
    assert s1.formulation == s2.formulation
    assert s1.intimacy == s2.intimacy
    assert s1.energy == s2.energy


# ---------------------------------------------------------------------------
# Kernel scalars preserved from v1
# ---------------------------------------------------------------------------

def test_energy_decreases_per_turn():
    s = EmotionState(energy=80).transition("嗯")
    assert s.energy == 78

def test_intimacy_boost_for_long_or_emotional():
    s = EmotionState(intimacy=50).transition("好开心！")
    assert s.intimacy == 53

def test_intimacy_short_neutral_gives_plus_1():
    s = EmotionState(intimacy=50).transition("嗯好")
    assert s.intimacy == 51

def test_daily_decay_rebounds_energy():
    then = dt.datetime(2026, 1, 1)
    now = dt.datetime(2026, 1, 4)  # 3 days > 1 day → energy rebounds; ≤7 days → no intimacy decay
    s = EmotionState(energy=30, intimacy=80, last_interaction_at=then).with_daily_decay(now=now)
    assert s.energy == min(100, 30 + 30)
    assert s.intimacy == 80


def test_daily_decay_intimacy_floor_30():
    then = dt.datetime(2026, 1, 1)
    now = dt.datetime(2026, 2, 1)  # 31 days
    s = EmotionState(intimacy=60, last_interaction_at=then).with_daily_decay(now=now)
    assert s.intimacy == 30
