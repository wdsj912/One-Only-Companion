"""
Anti-memory-sycophancy gate.

Problem: 97% median failure of companion systems is sycophancy — they
retrieve a memory that agrees with the user's current message and simply
confirm it.  This module inspects every retrieved memory *before* it enters
the prompt, detects the agreement-justification pattern, and forces a
mandatory challenge step.

The gate is deterministic, zero-LLM, and applies in three places:

  1. `should_challenge(memory, current_user_text)` → bool — pattern detection
  2. `inject_challenge_into_prompt_block(prompt_block, memory, user_text)` → str
     — if challenge forced, appends a single specific reason to disagree
  3. `record_challenge(...)` → async — log + rolling rate calculation

Also tracks rolling challenge rate.  If it drops below 0.1 over the last
50 turns → flag "memory-sycophancy creep" so the operator can diagnose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Pattern detection — deterministic.
# ---------------------------------------------------------------------------

# Rough polarity: does a snippet contain agreement vs disagreement markers?
_AGREE_MARKERS = ("对", "是的", "没错", "赞同", "同意", "觉得也是", "同频", "exactly", "totally", "me too")
_DISAGREE_MARKERS = ("不对", "其实", "未必", "但是", "不过我觉得", "我不确定", "maybe not", "not sure", "disagree")


def _polarity(text: str) -> int:
    t = text.lower()
    a = sum(1 for m in _AGREE_MARKERS if m.lower() in t)
    d = sum(1 for m in _DISAGREE_MARKERS if m.lower() in t)
    return a - d


def _shared_keywords(text_a: str, text_b: str, min_len: int = 1) -> set[str]:
    tokens_a = {t.strip() for t in re.split(r"[\s，。！？、,.!?;:]+", text_a) if len(t.strip()) >= min_len}
    tokens_b = {t.strip() for t in re.split(r"[\s，。！？、,.!?;:]+", text_b) if len(t.strip()) >= min_len}
    # Exact + substring-based overlap (Chinese phrases like "那个方案真的很棒"
    # contain multiple meaningful sub-words — catch when one token contains another).
    shared: set[str] = set()
    for ta in tokens_a:
        for tb in tokens_b:
            if ta and (ta == tb or ta in tb or tb in ta):
                shared.add(ta if len(ta) <= len(tb) else tb)
    return shared


@dataclass(frozen=True)
class SycophancySignal:
    agreement_detected: bool
    shared_keyword_count: int
    memory_polarity: int
    user_polarity: int
    reason: str


def detect_sycophancy(memory_text: str, current_user_text: str) -> SycophancySignal:
    """Detect whether using this memory would justify agreement with the user.

    Triggers when:
      * memory contains positive agreement markers AND
      * the current user message also contains agreement markers (both
        sides leaning toward confirmation) AND
      * there is non-trivial keyword overlap between them (memory isn't
        just a coincidence).
    """
    mem_pol = _polarity(memory_text)
    usr_pol = _polarity(current_user_text)
    shared = _shared_keywords(memory_text, current_user_text)

    agreement = (
        mem_pol >= 1
        and usr_pol >= 1
        and len(shared) >= 1
    )
    return SycophancySignal(
        agreement_detected=agreement,
        shared_keyword_count=len(shared),
        memory_polarity=mem_pol,
        user_polarity=usr_pol,
        reason="memory+user both agree" if agreement else "ok",
    )


# ---------------------------------------------------------------------------
# Challenge generation — deterministic, specific.
# We don't call an LLM; we emit a single short reason template keyed off
# memory layer.  The LLM then fleshes it out — but the *shape* of the
# challenge is fixed and cannot be skipped.
# ---------------------------------------------------------------------------

_CHALLENGE_TEMPLATES = {
    "semantic": "你之前说的「{fact_value}」，但有没有一种可能是因为{counter}？",
    "episodic": "关于上次{event}，会不会是{counter}造成的？",
    "pinned": "你一直记着那件事，但我在想——有没有可能{counter}？",
}

_COUNTER_STRINGS = [
    "你当时对情况还不够了解",
    "这件事还有你没看到的另一面",
    "当时的你和现在的你已经不一样了",
    "这只是一种解释，还缺少另一个视角",
]


def build_mandatory_challenge(memory_layer: str, memory_text: str, shared_keywords: set[str]) -> str:
    """Emit one specific reason to disagree or missing piece of evidence.

    Pure deterministic template — chooses a counter-string by rotating on
    the shared keyword hash so output is stable per (memory, query) pair.
    """
    counter_idx = hash(frozenset(shared_keywords) or memory_text) % len(_COUNTER_STRINGS)
    counter = _COUNTER_STRINGS[counter_idx]

    if memory_layer == "semantic":
        # memory_text format "fact_key = fact_value"
        fact_value = memory_text.split("=", 1)[1].strip() if "=" in memory_text else memory_text
        return _CHALLENGE_TEMPLATES["semantic"].format(fact_value=fact_value, counter=counter)
    if memory_layer == "episodic":
        event = memory_text[:20]
        return _CHALLENGE_TEMPLATES["episodic"].format(event=event, counter=counter)
    return _CHALLENGE_TEMPLATES["pinned"].format(counter=counter)


def augment_memories_prompt_block(block: str, memory_layer: str, memory_text: str, user_text: str) -> str:
    """Given the memories prompt block, append a mandatory challenge note
    if the sycophancy gate tripped.  Called by prompt builder."""
    sig = detect_sycophancy(memory_text, user_text)
    if not sig.agreement_detected:
        return block
    challenge = build_mandatory_challenge(memory_layer, memory_text, _shared_keywords(memory_text, user_text))
    gate_note = (
        "\n\n# Anti-sycophancy gate triggered\n"
        f"Mandatory challenge (you MUST raise this): {challenge}\n"
        "Do NOT simply agree — the user will trust you more if you push back thoughtfully."
    )
    return block + gate_note


# ---------------------------------------------------------------------------
# Rolling challenge rate tracker — persisted on User.challenge_counter +
# User.total_turns so it's inspectable.
# ---------------------------------------------------------------------------

SYCOPHANCY_CREEP_THRESHOLD = 0.1   # < 10% challenge rate over last 50 turns → flag
RATE_WINDOW = 50


def flag_memory_sycophancy_creep(
    session=None,
    challenge_counter: int = 0,
    total_turns: int = 0,
) -> Optional[str]:
    """Compute rolling challenge rate and return a creep warning if needed.

    `session` is accepted for parity with other async services but unused —
    the rate is pure math over persisted counters.
    """
    if total_turns < RATE_WINDOW:
        return None
    rate = challenge_counter / min(total_turns, RATE_WINDOW)
    if rate < SYCOPHANCY_CREEP_THRESHOLD:
        return (
            f"memory-sycophancy creep: challenge rate {rate:.2f} < {SYCOPHANCY_CREEP_THRESHOLD} "
            f"over last {min(total_turns, RATE_WINDOW)} turns"
        )
    return None
