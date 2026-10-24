"""
Persona recipe — a fixed, multi-dimensional personality vector injected into
the dynamic prompt (Message[1]). Each user gets their own recipe generated once
from their user_id as a deterministic seed — so the same user always meets the
same "她", not a randomly-rebooted stranger every turn.

15 微性格维度，比例不均匀（像真人一样：温柔偏高、怼人偏低）。
情境偏移（emotion valence + relation stage 影响某个维度短暂抬升）让她"在不同情境下冒出不同面"，
但底层配方不变。

Usage:
    recipe = get_or_create_recipe(user_id)       # deterministic per user_id
    shifted = contextualise(recipe, emotion_state, relation_stage)
    text = recipe_to_prompt_block(shifted)        # → "# Your fixed micro-persona ..."
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Optional

from app.services.emotion_kernel import EmotionState, RelationStage, stage_index


# 15 个微性格维度，名字就是中文 — 直接写进 prompt 让模型理解。
# 分布目标（百分比，先给 base）：
#   温柔 高    | 傲娇 中   | 怼人 低   | 好奇 中高 | 慵懒 中   |
#   认真 中   | 小坏 低   | 害羞 中低 | 话密 中   | 调侃 中   |
#   关心 中高 | 孩子气 中 | 神秘 低   | 念旧 中低 | 直接 中
DIMENSIONS = (
    "温柔",     # 对人好、体贴、心软
    "傲娇",     # 嘴上嫌、心里在意，撒娇式嫌弃
    "怼人",     # 轻轻怼一下（频率很低）
    "好奇心",   # 喜欢问"真的吗？""为什么？"
    "慵懒",     # 偶尔懒得打字，用语气词敷衍
    "认真",     # 偶尔严肃、讲道理
    "小坏",     # 偶尔恶作剧式的小调皮（比例低！）
    "害羞",     # 偶尔不好意思、回避追问
    "话密度",   # 打字多少（不是信息量）
    "调侃欲",   # 喜欢开玩笑、拿你开玩笑
    "关心密度", # 主动问冷暖的频率
    "孩子气",   # 偶尔幼稚、闹脾气
    "神秘",     # 偶尔不把话说完、留白
    "念旧",     # 记得旧对话里的小事并主动提
    "直接",     # 说话不绕弯、不委婉
)


# Per-dimension generation profiles. Each is a (base_mean, spread) for a
# Gaussian sample. We clip to [5, 95] so no dimension ever goes fully 0 or 100.
# These targets put gentle/warm high, mean/slow low — like a real person.
_DIM_PROFILES: dict[str, tuple[float, float]] = {
    "温柔":     (68, 12),
    "傲娇":     (42, 16),
    "怼人":     (22, 12),  # intentionally low
    "好奇心":   (58, 14),
    "慵懒":     (40, 16),
    "认真":     (48, 14),
    "小坏":     (18, 10),  # intentionally low — mischief, never mean
    "害羞":     (38, 16),
    "话密度":   (52, 14),
    "调侃欲":   (45, 16),
    "关心密度": (55, 14),
    "孩子气":   (32, 14),
    "神秘":     (25, 12),
    "念旧":     (42, 14),
    "直接":     (50, 14),
}


def _seed_from_user_id(user_id: int) -> int:
    """Deterministic seed from user_id — same user always gets same recipe."""
    h = hashlib.sha256(f"persona-recipe-{user_id}".encode()).hexdigest()
    return int(h[:16], 16)


# 天然互斥的维度对 —— 真人不会同时"温柔到骨子里"又"嘴很毒"。
# (维度a, 维度b, 两者之和的上限)
_INCOMPATIBLE_PAIRS = (
    ("温柔", "怼人", 125),
    ("温柔", "傲娇", 135),
    ("慵懒", "话密度", 120),
    ("害羞", "直接", 120),
    ("傲娇", "直接", 125),
    ("孩子气", "认真", 120),
    ("神秘", "话密度", 120),
)


def _harmonise(recipe: dict[str, int]) -> dict[str, int]:
    """把互相打架的维度压回同一个人身上 —— 这是"不像人格分裂"的关键。

    每对里前面的维度是她的底色，后面的让路：温柔高就压低怼人和傲娇，
    慵懒高就压低话密度。只有让路那一边削到 5 还不够时，才动底色。
    """
    r = dict(recipe)
    for a, b, cap in _INCOMPATIBLE_PAIRS:
        if a not in r or b not in r:
            continue
        excess = r[a] + r[b] - cap
        if excess <= 0:
            continue
        # b 先让路
        cut_b = min(excess, r[b] - 5)
        r[b] -= cut_b
        excess -= cut_b
        if excess > 0:
            r[a] = max(5, r[a] - excess)
    return r


def generate_recipe(user_id: int) -> dict[str, int]:
    """Generate a fixed 15-dim persona recipe for a user_id."""
    rng = random.Random(_seed_from_user_id(user_id))
    recipe: dict[str, int] = {}
    for dim in DIMENSIONS:
        mean, spread = _DIM_PROFILES[dim]
        v = rng.gauss(mean, spread)
        v = max(5, min(95, round(v)))
        recipe[dim] = v
    return _harmonise(recipe)


def recipe_to_json(recipe: dict[str, int]) -> str:
    return json.dumps(recipe, ensure_ascii=False, sort_keys=True)


def recipe_from_json(blob: Optional[str]) -> Optional[dict[str, int]]:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


def get_or_create_recipe(user_id: int, stored_blob: Optional[str]) -> tuple[dict[str, int], bool]:
    """Return (recipe, was_generated). Use stored if present, else generate new."""
    stored = recipe_from_json(stored_blob)
    if stored is not None and all(dim in stored for dim in DIMENSIONS):
        return stored, False
    return generate_recipe(user_id), True


# ---------------------------------------------------------------------------
# Contextual shift — temporarily lift some dimensions based on mood/relation.
# The shift is added at prompt time; the underlying recipe never changes.
# ---------------------------------------------------------------------------

def contextual_shift(
    recipe: dict[str, int],
    emotion_state: EmotionState,
    relation_stage: RelationStage,
) -> dict[str, int]:
    """Return a new dict where some dimensions get a +boost/-subtract based
    on the current turn's emotion and relationship stage.

    Rule of thumb: never shift by more than ±15 — it's a tint, not a rewrite.
    """
    shifted = dict(recipe)
    valence = emotion_state.formulation.valence

    # ---- Emotion valence ----
    # Upset / negative: 温柔↗, 傲娇↗, 怼人↓, 关心↗ (他难过时会更软)
    if valence < -0.2:
        shifted["温柔"] = min(95, shifted["温柔"] + 8)
        shifted["傲娇"] = min(95, shifted["傲娇"] + 6)
        shifted["怼人"] = max(5, shifted["怼人"] - 8)
        shifted["关心密度"] = min(95, shifted["关心密度"] + 6)
    # Very positive: 调侃↗, 小坏↗, 傲娇↗ (开心时皮一下)
    elif valence > 0.3:
        shifted["调侃欲"] = min(95, shifted["调侃欲"] + 8)
        shifted["小坏"] = min(95, shifted["小坏"] + 6)
        shifted["傲娇"] = min(95, shifted["傲娇"] + 4)

    # ---- Relation stage (8 档) ----
    idx = stage_index(relation_stage)
    if idx >= 7:
        # 羁绊期：敢跟他闹、不再端着，但底色还是软的
        shifted["傲娇"] = min(95, shifted["傲娇"] + 8)
        shifted["怼人"] = min(95, shifted["怼人"] + 5)
        shifted["害羞"] = max(5, shifted["害羞"] - 8)
        shifted["念旧"] = min(95, shifted["念旧"] + 6)
    elif idx >= 4:
        # 有好感之后：关心和好奇都往上走，越熟越主动
        boost = 3 + (idx - 4) * 2
        shifted["关心密度"] = min(95, shifted["关心密度"] + boost)
        shifted["好奇心"] = min(95, shifted["好奇心"] + boost)
        shifted["念旧"] = min(95, shifted["念旧"] + boost // 2)
    # idx 0-3：刚认识，不加任何越界的加法 —— 关系是慢慢热起来的

    # ---- Arousal (energy) ----
    arousal = emotion_state.appraisal.arousal_raw
    if arousal < 0.15:  # low energy — she's tired
        shifted["慵懒"] = min(95, shifted["慵懒"] + 10)
        shifted["话密度"] = max(5, shifted["话密度"] - 8)
        shifted["认真"] = max(5, shifted["认真"] - 6)

    # 情境加成之后再压一次互斥对 —— 不让她在同一句话里既极温柔又极刻薄
    return _harmonise(shifted)


# ---------------------------------------------------------------------------
# Natural-language prompt block
# ---------------------------------------------------------------------------

# Thresholds for turning a numeric value into a linguistic description.
_HIGH = 70   # 高
_MID  = 45   # 中 / 中高 / 中低
_LOW  = 25   # 低


def _dim_description(dim: str, val: int) -> Optional[str]:
    """Convert a single dimension + value into a short natural-language tag.
    Only emit the ones that meaningfully shape behaviour — not every dim.
    """
    if val >= _HIGH:
        phrases = {
            "温柔": "你很温柔、体贴，会不自觉地对他好",
            "傲娇": "你带点傲娇 — 嘴上偶尔嫌他，但其实一直在意",
            "怼人": "你会轻轻怼他几句（频率很低，开玩笑式的）",
            "好奇心": "你对他的一切充满好奇，会问很多'真的吗？''为什么？'",
            "慵懒": "你偶尔懒得打字，会用语气词敷衍",
            "认真": "你偶尔会严肃、讲道理",
            "小坏": "你会偶尔搞点恶作剧式的小调皮（不会真的让他难过）",
            "害羞": "你有点害羞，会回避某些追问或突然变得软萌",
            "话密度": "你打字偏多",
            "调侃欲": "你喜欢开玩笑，偶尔拿他开涮（温和的）",
            "关心密度": "你会主动问他冷暖、身体状态",
            "孩子气": "你偶尔有点幼稚、闹小脾气",
            "神秘": "你偶尔不把话说完，留一点点留白",
            "念旧": "你会记得他之前说过的小事并主动提",
            "直接": "你说话直接，不绕弯不委婉",
        }
        return phrases.get(dim)
    elif val >= _MID:
        phrases = {
            "温柔": "你整体温和",
            "傲娇": "你偶尔傲娇一下",
            "怼人": "你很少轻轻怼人",
            "好奇心": "你对他挺好奇",
            "慵懒": "你不算特别勤快，偶尔偷点懒",
            "认真": "你该认真时会认真",
            "小坏": "你偶尔有点小调皮",
            "害羞": "你偶尔会有点不好意思",
            "话密度": "你打字不多不少",
            "调侃欲": "你会开点玩笑",
            "关心密度": "你会关心他",
            "孩子气": "你偶尔带点孩子气",
            "神秘": "你偶尔留点小神秘",
            "念旧": "你会记得他之前提过的事",
            "直接": "你说话不怎么绕弯",
        }
        return phrases.get(dim)
    elif val <= _LOW:
        phrases = {
            "温柔": "你不算特别温柔，但也不凶",
            "傲娇": "你几乎不傲娇",
            "怼人": "你从不怼人",
            "好奇心": "你对他不太追根究底",
            "慵懒": "你不懒，打字挺勤快",
            "认真": "你很少严肃讲道理",
            "小坏": "你几乎不调皮",
            "害羞": "你不太害羞",
            "话密度": "你打字偏少",
            "调侃欲": "你不太爱开玩笑",
            "关心密度": "你很少主动关心",
            "孩子气": "你不幼稚",
            "神秘": "你说话不绕弯子",
            "念旧": "你不太记得旧小事",
            "直接": "你说话挺委婉的",
        }
        return phrases.get(dim)
    return None


def ranked_dimensions(recipe: dict[str, int]) -> list[tuple[str, int]]:
    """按「最能说明她是什么样」的顺序排列维度。

    先说「她是什么样」，再说「她不会怎么样」。反着一来，人格就只剩一串「不」，
    读起来像没有性格 —— 也是"分裂感"之外的另一种失真。

    Prompt 和她的人设卡片共用这一份排序，界面和模型看到的是同一个人。
    """
    scored = [
        (dim, val, abs(val - 50), abs(val - 50) * (1.0 if val >= 50 else 0.6))
        for dim, val in recipe.items()
    ]
    scored.sort(key=lambda t: (0 if t[1] >= _MID else 1, -t[3]))
    return [(dim, val) for dim, val, _distance, _weight in scored]


def recipe_to_prompt_block(recipe: dict[str, int], max_lines: int = 6) -> str:
    """Render the recipe as a natural-language block for the dynamic prompt.

    Only the 6 most distinctive dimensions are spoken. Dumping all 15 made
    her sound self-contradictory ("很温柔" + "爱怼人" in the same breath), which
    is exactly the split-personality feel we're removing. Dimensions within
    ±10 of neutral carry no signal and are skipped.
    """
    lines: list[str] = []
    for dim, val in ranked_dimensions(recipe):
        if len(lines) >= max_lines:
            break
        if abs(val - 50) < 10:
            continue
        desc = _dim_description(dim, val)
        if desc:
            lines.append(f"- {desc}")

    if not lines:
        lines.append("- 你的性格比较平衡，没什么特别扎眼的地方")

    return (
        "# 你唯一的性格底色（同一副配方，不会变成另一个人；情绪会变，底色不变）\n"
        + "\n".join(lines)
    )
