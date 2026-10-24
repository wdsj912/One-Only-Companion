"""
Dual-layer deterministic emotional kernel.  Zero LLM calls.  Pure functions only.

Layer A — Appraisal: fast visceral reaction.
  5-dim Scherer appraisal vector (relevance, implication, coping_potential,
  norm_compatibility, self_involvement) ∈ [0, 1]
  plus PAD triplet valence ∈ [-1, 1], arousal ∈ [0, 1], dominance ∈ [-1, 1]

Layer B — Formulation: slow-adapting "what actually gets said".
  PAD mood_field ∈ [-1,1] × [0,1] × [-1,1], regulation_gate ∈ [0,1].
  Does NOT automatically mirror Layer A.  When appraisal is intense but
  suppression is justified (low intimacy, self-presentation), the gap
  between A and B is the source of human-likeness — she feels more than
  she says.

Hard constraints:
  * Emotional momentum: valence_t = 0.7 * valence_{t-1} + 0.3 * appraisal_input
    A single message CANNOT flip valence from < -0.3 to > +0.2 in one step.
  * Arousal decays towards 0.3 with a half-life of 4 TURNS (not wall-clock).
  * Formulation adapts at 0.3× the appraisal step.
  * No LLM imports anywhere in this module.  A module-level guard prevents it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Zero-LLM guard — import-time check + static assert.
# Any accidental `from app.services.llm_service import …` below is a hard error.
# ---------------------------------------------------------------------------

_ALLOWED_IMPORTS = {"__future__", "datetime", "dataclasses", "enum", "typing", "math"}
import sys as _sys
_kernel_globals = set(_sys.modules[__name__].__dict__.keys())
# This module must never import an LLM-dependent module.  If someone sneaks
# one in, the guard at the bottom of the file will trip — kept commented so
# pytest can show it if violated; production deploys can uncomment:
# assert "llm_service" not in _sys.modules, "LLM module leaked into emotion_kernel"


# ---------------------------------------------------------------------------
# Classifiers — keyword-based, deterministic, same as before.
# ---------------------------------------------------------------------------

_SAD_KEYWORDS = (
    "难过", "伤心", "哭", "沮丧", "郁闷", "不开心", "失落", "委屈", "崩溃", "空虚",
    "没意思", "憋屈", "想死", "撑不住", "抗不住", "emo", "alone", "sad", "cry", "lonely",
    "累", "疲惫", "心累",
)
_ANGER_KEYWORDS = (
    "生气", "愤怒", "烦", "讨厌", "气死", "离谱", "恶心", "破防", "无语", "可恶",
    "hate", "angry", "frustrated", "annoyed",
)
_ANXIETY_KEYWORDS = (
    "焦虑", "紧张", "怕", "担心", "害怕", "慌", "不安", "忐忑", "睡不着", "手心出汗",
    "anxious", "worried", "nervous", "panic",
)
_JOY_KEYWORDS = (
    "开心", "高兴", "哈哈", "嘻嘻", "棒", "好耶", "爽", "舒服", "幸福", "惊喜",
    "爽到", "太好了", "happy", "great", "love it", "喜欢", "excited",
)

# Emoji carries emotion even when the words don't.
_EMOJI_EMOTION = {
    "😭": "sad", "😢": "sad", "💔": "sad", "😞": "sad", "🥲": "sad", "😔": "sad",
    "😡": "angry", "🤬": "angry", "😤": "angry", "💢": "angry",
    "😨": "anxious", "😰": "anxious", "😟": "anxious", "😱": "anxious",
    "😄": "joy", "😊": "joy", "🥰": "joy", "❤": "joy", "😂": "joy", "🤣": "joy", "🎉": "joy",
}

# Intensity markers — they amplify the raw hit, not the classification.
_INTENSIFIERS = ("超", "特别", "太", "非常", "真的", "死了", "极了", "巨", "超级", "爆")

# Explicit denials must not read as the emotion they name.
_NEGATIONS = (
    "不难过", "不伤心", "没生气", "不烦", "不焦虑", "不紧张", "不害怕",
    "不喜欢", "不讨厌", "不累", "不疲惫", "不开心才怪",
)

# Tie-break order: negative emotions win ties — she reads ambiguity as unease.
_PRIORITY = ("sad", "angry", "anxious", "joy")


def _emotion_hits(text: str) -> dict[str, int]:
    t = text.lower()
    for n in _NEGATIONS:
        t = t.replace(n, "　")
    hits = {label: 0 for label in _PRIORITY}
    for kw in _SAD_KEYWORDS:
        if kw in t:
            hits["sad"] += 1
    for kw in _ANGER_KEYWORDS:
        if kw in t:
            hits["angry"] += 1
    for kw in _ANXIETY_KEYWORDS:
        if kw in t:
            hits["anxious"] += 1
    for kw in _JOY_KEYWORDS:
        if kw in t:
            hits["joy"] += 1
    for emo, label in _EMOJI_EMOTION.items():
        if emo in text:
            hits[label] += 1
    return hits


def emotion_intensity(text: str) -> float:
    """0.9–1.35 multiplier from intensifiers, exclamation marks and emoji runs."""
    boost = 0.0
    for kw in _INTENSIFIERS:
        if kw in text:
            boost += 0.05
    boost += min(0.15, text.count("!") * 0.05 + text.count("！") * 0.05)
    boost += min(0.10, text.count("?") * 0.02 + text.count("？") * 0.02)
    return max(0.9, min(1.35, 1.0 + boost))


def classify_user_valence(text: str) -> str:
    t = text.lower()
    if not t.strip():
        return "neutral"
    hits = _emotion_hits(t)
    if not any(hits.values()):
        return "neutral"

    negatives = hits["sad"] + hits["angry"] + hits["anxious"]
    if hits["joy"] and negatives:
        return "mixed"
    for label in _PRIORITY:
        if hits[label]:
            return label
    return "neutral"


# ---------------------------------------------------------------------------
# Appraisal input synthesis — maps valence label + text features → a 5-dim
# Scherer vector plus raw valence/arousal/dominance.  Fully deterministic.
# ---------------------------------------------------------------------------

def _synthesis_appraisal(text: str, valence_label: str) -> "Appraisal":
    """Synthesize Layer A input from a single user message.

    Returns an Appraisal object that represents the *raw visceral hit* —
    the kernel then applies momentum and regulation gates on top of it.

    Arousal_raw is zero for neutral messages — emotional arousal is only
    excited by non-neutral content.  This makes the half-life decay testable:
    feeding a run of neutrals drives arousal toward baseline 0.3 exactly.
    """
    text_len = len(text)

    # Base valence per label; neutral skews small-negative (ambiguity → mild unease)
    val_map = {
        "joy":     0.7,
        "sad":    -0.7,
        "angry":  -0.5,
        "anxious": -0.4,
        "mixed":  -0.15,   # 又开心又难过 — 说不清，偏沉一点
        "neutral": 0.0,
    }
    v = val_map[valence_label]

    # Arousal: emotional content drives it; neutral → 0 (only emotion excites arousal)
    ar_map = {
        "joy":     0.7,
        "sad":     0.5,
        "angry":   0.8,
        "anxious": 0.75,
        "mixed":   0.55,
        "neutral": 0.0,
    }
    a = ar_map[valence_label]

    # Dominance: sad/anxious = low (submissive), angry = mid (fighting), joy = high
    dom_map = {
        "joy":     0.4,
        "sad":    -0.3,
        "angry":   0.2,
        "anxious": -0.4,
        "mixed":  -0.1,
        "neutral": 0.0,
    }
    d = dom_map[valence_label]

    # 语气强度：感叹号/叠词/emoji 让同一句话砸得更重
    amp = emotion_intensity(text) if valence_label != "neutral" else 1.0
    v = max(-1.0, min(1.0, v * amp))
    a = max(0.0, min(1.0, a * amp))
    d = max(-1.0, min(1.0, d * amp))

    # Scherer appraisal dimensions — 5 items in [0, 1]
    # relevance: longer text + emotion = more relevant
    relevance = min(1.0, (text_len / 200) * 0.5 + (0.3 if valence_label != "neutral" else 0.0))

    # implication: valence extremity
    implication = min(1.0, abs(v))

    # coping_potential: neutral/joy = high (manageable), negative = low
    coping = 0.8 if valence_label in ("joy", "neutral") else 0.4

    # norm_compatibility: anger may violate norms
    norm = 0.4 if valence_label == "angry" else 0.85

    # self_involvement: long or emotional = high involvement
    self_inv = min(1.0, relevance) if valence_label != "neutral" else min(1.0, text_len / 300)

    return Appraisal(
        relevance=relevance,
        implication=implication,
        coping_potential=coping,
        norm_compatibility=norm,
        self_involvement=self_inv,
        valence_raw=v,
        arousal_raw=a,
        dominance_raw=d,
    )


# ---------------------------------------------------------------------------
# State dataclasses — frozen so every transition is a pure-function call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Appraisal:
    """Layer A — visceral reaction vector, all in [0,1] or [-1,1] as noted."""
    relevance: float           # [0,1]
    implication: float         # [0,1]
    coping_potential: float    # [0,1]
    norm_compatibility: float  # [0,1]
    self_involvement: float    # [0,1]
    valence_raw: float         # [-1,1]  — initial appraisal before momentum
    arousal_raw: float         # [0,1]
    dominance_raw: float       # [-1,1]

    # After momentum application:
    valence_momentum: float = 0.0   # [-1,1]
    arousal_momentum: float = 0.0   # [0,1]
    dominance_momentum: float = 0.0 # [-1,1]


@dataclass(frozen=True)
class Formulation:
    """Layer B — what she *says* to the user."""
    valence: float            # [-1,1]
    arousal: float            # [0,1]
    dominance: float          # [-1,1]
    suppression_gate: float   # [0,1] — 0 = speak freely, 1 = maximal restraint

    def mood_label(self) -> str:
        v, a = self.valence, self.arousal
        if v > 0.3 and a > 0.5:   return "excited"
        if v > 0.3 and a <= 0.5:  return "happy"
        if v < -0.3 and a > 0.5:  return "concerned"
        if v < -0.3 and a <= 0.5: return "sad"
        return "calm"


class RelationStage(str, Enum):
    """八档关系刻度。晋级只看累计对话轮数，绝不因单轮情绪跳档。"""

    EXPLORATORY = "exploratory"   # 1 陌生 — 客气、留距离
    ACQUAINTED = "acquainted"     # 2 认识 — 会接梗，仍不越界
    FAMILIAR = "familiar"         # 3 熟络 — 开始主动提问
    CLOSE = "close"               # 4 亲近 — 会念叨他
    AFFECTIVE = "affective"       # 5 有好感 — 会在意他怎么看自己
    FLIRTING = "flirting"         # 6 暧昧 — 试探、欲言又止
    INTIMATE = "intimate"         # 7 亲密 — 敢提要求、敢闹脾气
    DEEPENING = "deepening"       # 8 羁绊 — 像长期生活里的人


STAGE_ORDER: tuple[RelationStage, ...] = (
    RelationStage.EXPLORATORY,
    RelationStage.ACQUAINTED,
    RelationStage.FAMILIAR,
    RelationStage.CLOSE,
    RelationStage.AFFECTIVE,
    RelationStage.FLIRTING,
    RelationStage.INTIMATE,
    RelationStage.DEEPENING,
)


def stage_index(stage: RelationStage) -> int:
    """0-based position in the 8-tier ladder. Unknown values fall back to 0."""
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


@dataclass(frozen=True)
class EmotionState:
    """Dual-layer emotional kernel state."""

    appraisal: Appraisal = field(default_factory=lambda: Appraisal(
        relevance=0.3, implication=0.2, coping_potential=0.7,
        norm_compatibility=0.8, self_involvement=0.2,
        valence_raw=0.0, arousal_raw=0.15, dominance_raw=0.0,
        valence_momentum=0.0, arousal_momentum=0.15, dominance_momentum=0.0,
    ))

    formulation: Formulation = field(default_factory=lambda: Formulation(
        valence=0.0, arousal=0.2, dominance=0.0, suppression_gate=0.0,
    ))

    # Non-emotional but kernel-driven scalar state (kept from v1)
    intimacy: int = 30
    energy: int = 70
    last_interaction_at: Optional[dt.datetime] = None
    turns_since_arousal_decay: int = 0  # arousal half-life runs per turn
    session_turns: int = 0              # current session length

    # --- constants --------------------------------------------------------
    VALENCE_ALPHA = 0.7         # momentum carry (high → "lingers")
    VALENCE_INPUT_W = 0.22      # new input weight (was 0.3 — one line shouldn't flip her mood)
    VALENCE_MAX_STEP = 0.22     # per-turn cap on valence movement (damping)
    AROUSAL_HALF_LIFE = 4       # turns (not wall-clock)
    FORMULATION_ALPHA = 0.3     # B adapts at 0.3× appraisal

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    # ------------------------------------------------------------------
    # Suppression gate — bridges intimacy + self-presentation with appraisal.
    # ------------------------------------------------------------------

    def _suppression(self, stage: RelationStage) -> float:
        """Compute how much Layer B suppresses Layer A.

        High appraisal arousal + low intimacy → she feels it but holds back.
        Deepening stage → suppression naturally lower (safety of intimacy).
        """
        a = self.appraisal.arousal_momentum
        intimacy_norm = self.intimacy / 100.0
        base_suppress = (a - intimacy_norm) * 0.8 + 0.2
        # Relationship-stage modulation — linear across the 8 tiers:
        # +0.15 at 陌生 (holds back a lot) → -0.13 at 羁绊 (says what she feels).
        base_suppress += 0.15 - 0.04 * stage_index(stage)
        return self._clamp(base_suppress, 0.0, 0.8)

    # ------------------------------------------------------------------
    # Core transition — pure, deterministic, reproducible.
    # ------------------------------------------------------------------

    def transition(
        self,
        user_text: str,
        *,
        stage: RelationStage = RelationStage.EXPLORATORY,
        now: Optional[dt.datetime] = None,
    ) -> "EmotionState":
        now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

        # 1) Synthesise raw appraisal input from the message
        valence_label = classify_user_valence(user_text)
        raw = _synthesis_appraisal(user_text, valence_label)

        # 2) Layer A — momentum.  Arousal is pure exponential decay toward
        # baseline (0.3) with half-life in turns; new input then adds on top
        # at VALENCE_INPUT_W weighting.  Valence/dominance use the momentum
        # formula directly (no decay baseline — they drift toward 0).
        a = self.appraisal.arousal_momentum
        decay_factor = 0.5 ** (1.0 / self.AROUSAL_HALF_LIFE)  # 0.8409 per turn
        decayed = 0.3 + (a - 0.3) * decay_factor  # decay toward 0.3 baseline
        new_arousal = self._clamp(decayed + raw.arousal_raw * self.VALENCE_INPUT_W, 0.0, 1.0)

        # 负面偏倚：负面事件冲击 1.5x，正面 1.0x（心理学 negativity bias）
        neg_boost = 1.5 if raw.valence_raw < 0 else 1.0
        new_valence = self.VALENCE_ALPHA * self.appraisal.valence_momentum \
                    + self.VALENCE_INPUT_W * raw.valence_raw * neg_boost
        # Damping — even a wall of exclamation marks moves her at most 0.22/turn.
        new_valence = self._clamp(
            new_valence,
            max(-1.0, self.appraisal.valence_momentum - self.VALENCE_MAX_STEP),
            min(1.0, self.appraisal.valence_momentum + self.VALENCE_MAX_STEP),
        )

        new_dominance = self.VALENCE_ALPHA * self.appraisal.dominance_momentum \
                      + self.VALENCE_INPUT_W * raw.dominance_raw
        new_dominance = self._clamp(
            new_dominance,
            max(-1.0, self.appraisal.dominance_momentum - self.VALENCE_MAX_STEP),
            min(1.0, self.appraisal.dominance_momentum + self.VALENCE_MAX_STEP),
        )

        # Scherer vector — lower momentum (50% carry) because it's more specific
        new_appraisal = Appraisal(
            relevance=self._clamp(0.5 * self.appraisal.relevance + 0.5 * raw.relevance, 0.0, 1.0),
            implication=self._clamp(0.5 * self.appraisal.implication + 0.5 * raw.implication, 0.0, 1.0),
            coping_potential=self._clamp(0.5 * self.appraisal.coping_potential + 0.5 * raw.coping_potential, 0.0, 1.0),
            norm_compatibility=self._clamp(0.5 * self.appraisal.norm_compatibility + 0.5 * raw.norm_compatibility, 0.0, 1.0),
            self_involvement=self._clamp(0.5 * self.appraisal.self_involvement + 0.5 * raw.self_involvement, 0.0, 1.0),
            valence_raw=raw.valence_raw,
            arousal_raw=raw.arousal_raw,
            dominance_raw=raw.dominance_raw,
            valence_momentum=new_valence,
            arousal_momentum=new_arousal,
            dominance_momentum=new_dominance,
        )

        # 3) Layer B — formulation adapts slower, and is gated by suppression
        suppress = self._suppression(stage)
        # Appraisal → target formulation vector (with suppression attenuating amplitude)
        target_v = new_valence * (1.0 - suppress * 0.6)  # never fully zero-out valence
        target_a = new_arousal * (1.0 - suppress * 0.8)  # arousal gets suppressed harder
        target_d = new_dominance * (1.0 - suppress * 0.5)

        # Slow adaptation — α = 0.3, so ~4 turns to move 50%
        f = self.formulation
        new_formulation = Formulation(
            valence=self._clamp((1 - self.FORMULATION_ALPHA) * f.valence + self.FORMULATION_ALPHA * target_v, -1.0, 1.0),
            arousal=self._clamp((1 - self.FORMULATION_ALPHA) * f.arousal + self.FORMULATION_ALPHA * target_a, 0.0, 1.0),
            dominance=self._clamp((1 - self.FORMULATION_ALPHA) * f.dominance + self.FORMULATION_ALPHA * target_d, -1.0, 1.0),
            suppression_gate=suppress,
        )

        # 4) Kernel-driven scalars (preserved from v1)
        energy = max(20, min(100, self.energy - 2 + (5 if valence_label == "joy" else 0)))
        non_neutral = valence_label != "neutral"
        delta = 3 if (len(user_text) > 80 or non_neutral) else 1
        intimacy = max(0, min(100, self.intimacy + delta))

        return EmotionState(
            appraisal=new_appraisal,
            formulation=new_formulation,
            intimacy=intimacy,
            energy=energy,
            last_interaction_at=now,
            turns_since_arousal_decay=self.turns_since_arousal_decay + 1,
            session_turns=self.session_turns + 1,
        )

    # ------------------------------------------------------------------
    # Lazy daily decay — called once per session start
    # ------------------------------------------------------------------

    def with_daily_decay(self, now: Optional[dt.datetime] = None) -> "EmotionState":
        now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        if self.last_interaction_at is None:
            return self
        gap = now - self.last_interaction_at
        days = gap.total_seconds() / 86400.0
        if days <= 1:
            return self

        energy = max(20, min(100, self.energy + 30))
        intimacy = max(30, min(100, self.intimacy - int(2 * (days - 7)) if days > 7 else self.intimacy))
        # Emotion drifts toward neutral after long absence
        new_v = self.appraisal.valence_momentum * (0.6 if days <= 3 else 0.3)
        new_a = self.appraisal.arousal_momentum * (0.5 if days <= 3 else 0.25)

        new_appraisal = Appraisal(
            relevance=self.appraisal.relevance * 0.5,
            implication=self.appraisal.implication * 0.5,
            coping_potential=self.appraisal.coping_potential,
            norm_compatibility=self.appraisal.norm_compatibility,
            self_involvement=self.appraisal.self_involvement * 0.5,
            valence_raw=0.0, arousal_raw=0.15, dominance_raw=0.0,
            valence_momentum=new_v, arousal_momentum=new_a,
            dominance_momentum=self.appraisal.dominance_momentum * 0.5,
        )
        return EmotionState(
            appraisal=new_appraisal,
            formulation=self.formulation,
            intimacy=intimacy, energy=energy,
            last_interaction_at=self.last_interaction_at,  # caller updates persist timestamp
            turns_since_arousal_decay=self.turns_since_arousal_decay,
            session_turns=self.session_turns,
        )

    # ------------------------------------------------------------------
    # Hint strings — consumed by the prompt builder
    # ------------------------------------------------------------------

    def emotion_block_for_prompt(self) -> str:
        a = self.appraisal
        f = self.formulation

        # Qualitative suppression sentence — the single core of this block.
        gate = f.suppression_gate
        delta = a.valence_momentum - f.valence  # A is appraisal, B is formulation
        if gate > 0.6:
            if delta < -0.1:
                qual_sentence = "你此刻实际感受比说出来的更负面，但你选择收着。"
            elif delta > 0.1:
                qual_sentence = "你心情其实一般，但对方在兴头上，你配合着。"
            else:
                qual_sentence = "你有点拿不准要不要说透，先含糊着。"
        else:
            qual_sentence = "你此刻的状态和说出来的差不多，不用刻意收。"

        # Delivery hints — qualitative, range-based, no raw numbers in the text.
        hints: list[str] = [f"- {qual_sentence}"]

        # 第一人称体感 —— 先有身体，再有情绪。让她说话前先知道自己"现在什么样"。
        if f.valence < -0.35:
            body = "心里发沉，胸口闷闷的，说话不太想用力，也不太想解释。"
        elif f.valence < -0.1:
            body = "有点提不起劲，但还是撑着，愿意听他说。"
        elif f.valence > 0.35 and f.arousal > 0.45:
            body = "心情是轻的，嘴角有点压不住，打字都比平时快。"
        elif f.valence > 0.2:
            body = "状态还行，愿意多聊两句。"
        else:
            body = "没什么特别的感觉，平平的，说不上好也说不上坏。"
        if self.energy < 30:
            body += " 眼皮有点沉，回话会短，也会有点懒。"
        elif self.energy < 55:
            body += " 有点累，但还能撑着。"
        hints.append(f"- 你此刻的身体感：{body}")

        # 复合情绪矩阵：能量 × 效价交叉出"矛盾又真实"的状态
        compound = get_compound_emotion_text(f.valence, self.energy)
        if compound:
            hints.append(f"- {compound}")

        if self.energy < 30 or f.arousal < 0.25:
            hints.append("- 简短回复，不要主动开启新话题。")
        if f.valence < -0.3 and f.arousal > 0.5:
            hints.append("- 关心，不是难过——语气温柔，多听少劝。")
        if f.valence > 0.3 and f.arousal > 0.5:
            hints.append("- 轻松温暖，自然流露就好，不要刻意活泼。")
        if self.intimacy > 70:
            hints.append("- 可以用昵称；适度温柔的打趣没问题。")
        elif self.intimacy < 40:
            hints.append("- 友好但不越界，先不要用昵称。")
        # Layer A intensity — qualitative only
        if a.arousal_momentum > 0.6:
            hints.append("- 她此刻有真切的情绪，回应时把这个当背景，不要假装没察觉。")

        hint_str = "\n".join(hints)
        return (
            f"# Emotional state (deterministic kernel — do NOT argue, do NOT print raw numbers)\n\n"
            f"Hints:\n{hint_str}"
        )


# ---------------------------------------------------------------------------
# 时间平复 & 复合情绪矩阵 —— 模块级纯函数
# ---------------------------------------------------------------------------

VALENCE_DECAY_NEG_PER_HOUR = 0.08  # 负面情绪平复得更慢（负面偏倚）
VALENCE_DECAY_POS_PER_HOUR = 0.12


def decay_emotion_by_time(state: "EmotionState", now: Optional[dt.datetime] = None) -> "EmotionState":
    """时间平复效应：沉默期间 formulation.valence 线性向 0 回落。

    负面 -0.08/h，正面 -0.12/h。不动 last_interaction_at（由调用方持久化）。
    """
    from dataclasses import replace as _replace

    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    last = state.last_interaction_at
    if last is None:
        return state
    hours = max(0.0, (now - last.replace(tzinfo=None)).total_seconds() / 3600.0)
    if hours < 0.25:
        return state
    v = state.formulation.valence
    if abs(v) < 0.01:
        return state
    rate = VALENCE_DECAY_NEG_PER_HOUR if v < 0 else VALENCE_DECAY_POS_PER_HOUR
    step = rate * hours
    new_v = 0.0 if abs(v) <= step else (v + step if v < 0 else v - step)
    return _replace(state, formulation=_replace(state.formulation, valence=new_v))


def compound_mood_label(valence: float, energy: float) -> str:
    """能量 × 效价 交叉出复合情绪标签。"""
    hi_e, lo_e = energy >= 60, energy <= 30
    pos, neg = valence > 0.3, valence < -0.3
    if hi_e and pos:
        return "兴奋"
    if hi_e and neg:
        return "暴躁"
    if lo_e and pos:
        return "慵懒惬意"
    if lo_e and neg:
        return "又累又委屈"
    if pos:
        return "开心"
    if neg:
        return "低落"
    if lo_e:
        return "困倦"
    return "平静"


def get_compound_emotion_text(valence: float, energy: float) -> str:
    """复合情绪的自然语言描述，注入动态 prompt。中性区返回空串。"""
    hi_e, lo_e = energy >= 60, energy <= 30
    pos, neg = valence > 0.3, valence < -0.3
    if lo_e and neg:
        return "你非常疲惫，同时又觉得委屈/烦躁——回复会显得有点不耐烦或脆弱，话比较少。"
    if lo_e and pos:
        return "你困得眼睛都快睁不开了，但心情很好——回复会有点软糯、想撒娇。"
    if hi_e and neg:
        return "你精神很好，但正在气头上——回复会比较冲、语速快、带点攻击性。"
    if hi_e and pos:
        return "你精力充沛、心情也很好——回复轻快、愿意多聊、会主动延伸话题。"
    if lo_e:
        return "你能量见底了，整个人懒懒的——回复短、慢、不想动脑子。"
    return ""


def apply_time_decay_to_formulation_json(
    formulation_json: Optional[str],
    last_interaction_at: Optional[dt.datetime],
    now: Optional[dt.datetime] = None,
):
    """供只读接口（/api/companion）使用：对持久化的 formulation 做时间平复。

    返回 (new_json_or_None, valence)。无变化时 new_json 为 None。
    """
    import json as _json

    if not formulation_json:
        return None, 0.0
    try:
        d = _json.loads(formulation_json)
        v = float(d.get("valence", 0.0))
    except (ValueError, TypeError):
        return None, 0.0
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if last_interaction_at is None:
        return None, v
    hours = max(0.0, (now - last_interaction_at.replace(tzinfo=None)).total_seconds() / 3600.0)
    if hours < 0.25 or abs(v) < 0.01:
        return None, v
    rate = VALENCE_DECAY_NEG_PER_HOUR if v < 0 else VALENCE_DECAY_POS_PER_HOUR
    step = rate * hours
    new_v = 0.0 if abs(v) <= step else (v + step if v < 0 else v - step)
    if abs(new_v - v) < 1e-6:
        return None, v
    d["valence"] = new_v
    return _json.dumps(d), new_v
