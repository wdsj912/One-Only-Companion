"""Anti-memory-sycophancy gate tests."""
from __future__ import annotations

import pytest

from app.services.sycophancy_gate import (
    detect_sycophancy,
    SycophancySignal,
    build_mandatory_challenge,
    augment_memories_prompt_block,
    flag_memory_sycophancy_creep,
    SYCOPHANCY_CREEP_THRESHOLD,
)


def test_detects_agreement_both_sides():
    sig = detect_sycophancy(
        memory_text="上次你说对，那个方案真的很棒",
        current_user_text="对，我现在还是觉得那个方案真的很棒",
    )
    assert sig.agreement_detected
    assert sig.shared_keyword_count >= 1


def test_no_detect_when_memory_disagrees():
    sig = detect_sycophancy(
        memory_text="上次你其实觉得不对，方案有问题",
        current_user_text="对，我现在还是觉得那个方案真的很棒",
    )
    assert not sig.agreement_detected


def test_no_detect_when_user_disagrees():
    sig = detect_sycophancy(
        memory_text="你之前说那个方案很棒",
        current_user_text="其实我觉得它不好",
    )
    assert not sig.agreement_detected


def test_no_detect_when_no_overlap():
    sig = detect_sycophancy(
        memory_text="你上周五喝了冰美式",
        current_user_text="今天天气真不错",
    )
    assert not sig.agreement_detected


def test_build_mandatory_challenge_is_specific():
    challenge = build_mandatory_challenge("semantic", "user.like = 冰美式", {"冰美式"})
    assert "冰美式" in challenge
    assert "可能" in challenge or "有没有" in challenge


def test_challenge_injected_into_prompt_only_on_detect():
    clean_block = "# What you remember\n- hello"
    # No sycophancy — block unchanged
    result = augment_memories_prompt_block(clean_block, "semantic", "hello", "不相关的")
    assert result == clean_block

    # Sycophancy — challenge appended
    result = augment_memories_prompt_block(clean_block, "semantic",
        memory_text="你说过那个方案很棒 对 没错 赞同",
        user_text="对，我现在还是觉得那个方案很棒")
    assert "# Anti-sycophancy gate triggered" in result
    assert "Mandatory challenge" in result


def test_creep_flag_triggers_below_threshold():
    # 50 turns with only 3 challenges → rate = 0.06 < 0.10
    w = flag_memory_sycophancy_creep(challenge_counter=3, total_turns=50)
    assert w is not None
    assert "sycophancy creep" in w.lower()

    # 50 turns with 10 challenges → rate = 0.20 > 0.10 → ok
    assert flag_memory_sycophancy_creep(challenge_counter=10, total_turns=50) is None

    # Less than window → no flag yet
    assert flag_memory_sycophancy_creep(challenge_counter=0, total_turns=20) is None
