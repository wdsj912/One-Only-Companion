"""Prompt builder tests — verifies ordering + injection of dual-layer emotion state.

STEP 2 (Change B): No numeric leakage into the prompt. All emotion assertions
are qualitative.
"""
from __future__ import annotations

import datetime as dt
import re

from app import models as m
from app.services.emotion_kernel import EmotionState, Appraisal, Formulation, RelationStage
from app.services.prompt_builder import build_system_prompt, build_messages_array


def _mk_persona():
    return m.PersonaState(warmth=0.6, playfulness=0.35, directness=0.55, protectiveness=0.4)


def _mk_reflections():
    return [
        m.SelfReflection(content="上次我过度分析了用户的情绪", created_at=dt.datetime(2026, 10, 19)),
        m.SelfReflection(content="没注意到用户在回避这个话题", created_at=dt.datetime(2026, 10, 18)),
    ]


def _mk_memories():
    return [
        m.Memory(content="user.like = 冰美式", layer="semantic", emotion=None, intensity=3),
        m.Memory(content="用户说工作很辛苦", layer="episodic", emotion="sad", intensity=4),
        m.Memory(content="最重要的一次对话", layer="pinned", emotion="joy", intensity=5, pinned=True),
    ]


def test_system_prompt_order_has_all_sections():
    """Every spec-mandated block appears in order 1..6 inside the prompt."""
    prompt = build_system_prompt(
        companion_name="小禾",
        emotion_state=EmotionState(energy=60, intimacy=50),
        persona=_mk_persona(),
        reflections=_mk_reflections(),
        memories=_mk_memories(),
    )

    markers = [
        "Identity",                              # 1. static persona
        "Emotional state",                       # 2. emotion block
        "（同一副配方",                          # 3. fixed persona recipe (single source)
        "Recent self-corrections",               # 4. reflections
        "What you remember about the user",      # 5. memories
        "Safety & Boundaries",                   # 6. safety clause
    ]
    positions = [prompt.find(m) for m in markers]
    assert all(p != -1 for p, _ in zip(positions, markers)), f"missing markers: {list(zip(markers, positions))}"
    assert positions == sorted(positions), f"wrong order: {list(zip(markers, positions))}"


def test_emotion_state_injected_into_prompt():
    """Assert qualitative sentences are present AND raw numeric leakage is absent."""
    # gate > 0.6, appraisal more negative than formulation → suppression sentence 1
    state = EmotionState(
        appraisal=Appraisal(
            relevance=0.6, implication=0.5, coping_potential=0.4,
            norm_compatibility=0.8, self_involvement=0.7,
            valence_raw=-0.7, arousal_raw=0.6, dominance_raw=-0.3,
            valence_momentum=-0.5, arousal_momentum=0.4, dominance_momentum=-0.2,
        ),
        formulation=Formulation(valence=-0.2, arousal=0.25, dominance=-0.1, suppression_gate=0.75),
        intimacy=80, energy=25,
    )
    prompt = build_system_prompt(
        companion_name="小禾",
        emotion_state=state,
        persona=_mk_persona(),
        reflections=[], memories=[],
    )

    # (a) One of four qualitative suppression sentences must be present
    QUALITATIVE_SENTENCES = (
        "你此刻实际感受比说出来的更负面，但你选择收着。",
        "你心情其实一般，但对方在兴头上，你配合着。",
        "你有点拿不准要不要说透，先含糊着。",
        "你此刻的状态和说出来的差不多，不用刻意收。",
    )
    assert any(s in prompt for s in QUALITATIVE_SENTENCES), (
        f"expected a qualitative suppression sentence, prompt head: {prompt[:200]!r}"
    )

    # (b) No numeric leakage — these substrings must be absent
    NUMERIC_LEAK = ("Scherer", "rel=", "impl=", "cop=", "norm=", "self=", "suppression=")
    for needle in NUMERIC_LEAK:
        assert needle not in prompt, f"numeric leakage found: {needle!r}"

    # (c) Delivery hints must be present (qualitative)
    assert "简短回复" in prompt   # energy < 30
    assert "昵称" in prompt       # intimacy > 70 (or "不要用昵称" for low intimacy)


def test_prompt_has_no_raw_floats_in_emotion_block():
    """Regex the emotion block section only — no -?d+.dd patterns may appear there."""
    state = EmotionState(
        appraisal=Appraisal(
            relevance=0.5, implication=0.5, coping_potential=0.7,
            norm_compatibility=0.8, self_involvement=0.3,
            valence_raw=0.0, arousal_raw=0.0, dominance_raw=0.0,
            valence_momentum=-0.4, arousal_momentum=0.6, dominance_momentum=-0.2,
        ),
        formulation=Formulation(valence=-0.1, arousal=0.4, dominance=-0.1, suppression_gate=0.7),
        intimacy=85, energy=28,
    )
    prompt = build_system_prompt(
        companion_name="小禾",
        emotion_state=state,
        persona=_mk_persona(),
        reflections=[], memories=[],
    )

    # Isolate the emotion block between "# Emotional state" and the next "# " heading
    start = prompt.find("# Emotional state")
    assert start != -1, "emotion block missing"
    next_heading = prompt.find("\n# ", start + 1)
    end = next_heading if next_heading != -1 else len(prompt)
    emotion_block = prompt[start:end]

    # Any float like -0.50, 0.70, +0.2 etc. — zero matches inside emotion block
    FLOAT_RE = re.compile(r"-?\d+\.\d{2,}")
    raw_floats = FLOAT_RE.findall(emotion_block)
    assert len(raw_floats) == 0, (
        f"emotion block leaked raw floats: {raw_floats}\n"
        f"--- emotion block ---\n{emotion_block}\n----------------"
    )


def test_relation_stage_injected():
    prompt_exp = build_system_prompt(
        companion_name="小禾", emotion_state=EmotionState(), persona=_mk_persona(),
        reflections=[], memories=[], relation_stage=RelationStage.EXPLORATORY,
    )
    prompt_aff = build_system_prompt(
        companion_name="小禾", emotion_state=EmotionState(), persona=_mk_persona(),
        reflections=[], memories=[], relation_stage=RelationStage.AFFECTIVE,
    )
    prompt_dep = build_system_prompt(
        companion_name="小禾", emotion_state=EmotionState(), persona=_mk_persona(),
        reflections=[], memories=[], relation_stage=RelationStage.DEEPENING,
    )
    assert "1/8 陌生" in prompt_exp
    assert "5/8 有好感" in prompt_aff
    assert "8/8 羁绊" in prompt_dep


def test_persona_uses_single_recipe_source():
    """人格只有一份：固定 15 维配方。旧的 warmth/playfulness 数值块不再注入，
    避免两套性格参数互相打架导致「人格分裂感」。"""
    persona = m.PersonaState(user_id=7, warmth=0.72, playfulness=0.3, directness=0.68, protectiveness=0.32)
    prompt = build_system_prompt(
        companion_name="小禾", emotion_state=EmotionState(), persona=persona,
        reflections=[], memories=[],
    )
    assert "你唯一的性格底色" in prompt
    assert "warmth: 0.72" not in prompt
    assert "slowly drifting persona traits" not in prompt


def test_companion_profile_uses_same_recipe_ranking():
    """人设卡片和提示词读同一份配方、同一个排序 —— 界面和她自己认知里是同一个人。"""
    from app.routers.companion import _persona_profile
    from app.services import persona_recipe as pr

    persona = m.PersonaState(user_id=7)
    profile = _persona_profile(persona)

    recipe, _ = pr.get_or_create_recipe(7, None)
    assert len(profile) == 4
    assert [(p.label, p.value) for p in profile] == pr.ranked_dimensions(recipe)[:4]


def test_memory_block_uses_strict_type_prefixes():
    """Dual-Agent format: [Fact] / [Preference] / [Event], nothing else."""
    mems = [
        m.Memory(content="The user's name is Azhe", layer="semantic", fact_key="user.name", intensity=3),
        m.Memory(content="The user hates rainy days", layer="semantic", fact_key="preference.weather", intensity=3),
        m.Memory(content="The user went to the zoo today", layer="episodic", intensity=3),
    ]
    prompt = build_system_prompt(
        companion_name="小禾", emotion_state=EmotionState(), persona=_mk_persona(),
        reflections=[], memories=mems,
    )
    assert "[Fact] The user's name is Azhe" in prompt
    assert "[Preference] The user hates rainy days" in prompt
    assert "[Event] The user went to the zoo today" in prompt


def test_messages_array_ordering():
    msgs = [
        m.Message(role="user", content="hi", created_at=dt.datetime(2026, 10, 21, 10, 0)),
        m.Message(role="assistant", content="hii", created_at=dt.datetime(2026, 10, 21, 10, 1)),
    ]
    array = build_messages_array(static_prompt="STATIC", dynamic_prompt="DYNAMIC", working_memory=msgs)
    assert array[0] == {"role": "system", "content": "STATIC"}
    assert array[1] == {"role": "system", "content": "DYNAMIC"}
    assert array[2] == {"role": "user", "content": "hi"}
    assert array[3] == {"role": "assistant", "content": "hii"}


def test_working_memory_truncates_to_20():
    msgs = [m.Message(role="user", content=str(i), created_at=dt.datetime(2026, 10, 21, 10, i)) for i in range(25)]
    array = build_messages_array(static_prompt="S1", dynamic_prompt="S2", working_memory=msgs)
    # 2 system + 20 history = 22 total
    assert len(array) == 22
    # oldest history message should be index 2, content "5"
    assert array[2]["content"] == "5"
    assert array[-1]["content"] == "24"
