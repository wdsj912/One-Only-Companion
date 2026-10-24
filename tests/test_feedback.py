"""CTEM closed-loop tests — feedback, neutral streak, relation stage."""
from __future__ import annotations

import pytest

from app.services.emotion_kernel import EmotionState, Appraisal, Formulation, RelationStage
from app.services.feedback import (
    FeedbackSignal,
    extract_feedback,
    apply_feedback_to_state,
    transition_relation_stage,
)


class TestFeedbackExtraction:
    def test_follow_up_is_positive(self):
        sig = extract_feedback(0.0, 0.3, 10, 10, True, False)
        assert sig.follow_up
        assert not sig.is_neutral

    def test_topic_abandoned_is_negative(self):
        sig = extract_feedback(0.0, -0.1, 20, 5, False, True)
        assert sig.topic_abandoned
        assert not sig.is_neutral

    def test_small_changes_count_as_neutral(self):
        sig = extract_feedback(0.0, 0.0, 10, 11, False, False)
        assert sig.is_neutral


class TestNeutralStreak:
    def test_three_neutrals_drift_formulation_down(self):
        """3 consecutive neutrals → mild negative: formulation valence drops slightly."""
        state = EmotionState(
            appraisal=Appraisal(
                relevance=0.4, implication=0.3, coping_potential=0.7,
                norm_compatibility=0.8, self_involvement=0.3,
                valence_raw=0.0, arousal_raw=0.0, dominance_raw=0.0,
                valence_momentum=0.0, arousal_momentum=0.2, dominance_momentum=0.0,
            ),
            formulation=Formulation(valence=0.15, arousal=0.2, dominance=0.0, suppression_gate=0.2),
        )
        # 1st neutral — no effect yet
        sig = FeedbackSignal(0.0, 0.0, False, False, True)
        s1 = apply_feedback_to_state(state, sig, neutral_streak=1)
        assert s1.formulation.valence == pytest.approx(0.15, abs=0.001)

        # 3rd neutral — kicks in
        s3 = apply_feedback_to_state(state, sig, neutral_streak=3)
        assert s3.formulation.valence < state.formulation.valence - 0.02  # dropped by at least 0.03
        assert s3.formulation.suppression_gate > state.formulation.suppression_gate

    def test_followup_warms_formulation(self):
        state = EmotionState(formulation=Formulation(valence=-0.05, arousal=0.2, dominance=0.0, suppression_gate=0.2))
        sig = FeedbackSignal(0.0, 0.3, True, False, False)
        new = apply_feedback_to_state(state, sig, neutral_streak=0)
        assert new.formulation.valence > state.formulation.valence

    def test_topic_abandon_cools_formulation(self):
        state = EmotionState(formulation=Formulation(valence=0.2, arousal=0.3, dominance=0.0, suppression_gate=0.1))
        sig = FeedbackSignal(0.0, 0.0, False, True, False)
        new = apply_feedback_to_state(state, sig, neutral_streak=0)
        assert new.formulation.valence < state.formulation.valence - 0.03
        assert new.formulation.suppression_gate > state.formulation.suppression_gate


class TestRelationStageTransition:
    """八档刻度：100 / 300 / 600 / 1000 / 1500 / 2100 / 2800 轮。"""

    def test_eight_tier_ladder(self):
        cases = [
            (0, RelationStage.EXPLORATORY),
            (99, RelationStage.EXPLORATORY),
            (100, RelationStage.ACQUAINTED),
            (299, RelationStage.ACQUAINTED),
            (300, RelationStage.FAMILIAR),
            (599, RelationStage.FAMILIAR),
            (600, RelationStage.CLOSE),
            (999, RelationStage.CLOSE),
            (1000, RelationStage.AFFECTIVE),
            (1499, RelationStage.AFFECTIVE),
            (1500, RelationStage.FLIRTING),
            (2099, RelationStage.FLIRTING),
            (2100, RelationStage.INTIMATE),
            (2799, RelationStage.INTIMATE),
            (2800, RelationStage.DEEPENING),
            (99999, RelationStage.DEEPENING),
        ]
        for turns, expected in cases:
            assert transition_relation_stage(RelationStage.EXPLORATORY, turns) == expected, turns

    def test_there_are_exactly_eight_stages(self):
        from app.services.emotion_kernel import STAGE_ORDER

        assert len(STAGE_ORDER) == 8

    def test_never_regresses(self):
        assert transition_relation_stage(RelationStage.DEEPENING, 0) == RelationStage.DEEPENING
        assert transition_relation_stage(RelationStage.INTIMATE, 100) == RelationStage.INTIMATE

    def test_single_turn_cannot_jump_more_than_one_tier(self):
        """关系不可能一夜之间从陌生跳到亲密 —— 阈值本身就是硬门槛。"""
        assert transition_relation_stage(RelationStage.EXPLORATORY, 1) == RelationStage.EXPLORATORY
        # 一步一个台阶：从 0 开始，100 轮只到第二档
        assert transition_relation_stage(RelationStage.EXPLORATORY, 100) == RelationStage.ACQUAINTED
