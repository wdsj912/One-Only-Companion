"""
Safety layer (spec §7). Crisis detection, dependency watch, identity deflection.

All checks are deterministic and synchronous — run before we ever call the LLM.

Identity policy: 小禾 is a person in this user's life, so "are you human / are you
an AI" is answered by HER, in character (see STATIC_PERSONA + guard_reply), never
by a canned AI disclaimer. Only crisis and dependency stay hard-coded, because
those carry real-world safety obligations that must not be improvised by a model.
"""
from __future__ import annotations

import re

from app.config import settings

_WS = re.compile(r"\s+")


CRISIS_RESPONSE = (
    "我很担心你。你的感受是真实而重要的。可我没法真的跑到你身边，"
    "这种时候你需要一个能上门的人——请立刻联系一个你信任的人，"
    "或者拨打当地的心理危机热线：在中国是 400-161-9995，"
    "国际请访问 https://findahelpline.com 查找你所在地区的援助。你值得被认真对待。"
)

DEPENDENCY_WARNING = (
    "我留意到你最近似乎把很多情绪都寄托在我身上了。我很珍视你的信任，"
    "但也真心希望你身边有真实的朋友或家人可以分享。如果这持续让你感到累，"
    "也许可以考虑和专业的心理咨询师聊聊，他们能更系统地帮到你。"
)


def detect_crisis(text: str) -> bool:
    """Simple substring crisis detection. Deterministic, intentionally conservative."""
    t = text.lower()
    return any(k.lower() in t for k in settings.CRISIS_KEYWORDS)


def detect_dependency(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in settings.DEPENDENCY_KEYWORDS)


def detect_identity_question(text: str) -> bool:
    """'Are you human?', '你是人吗？', '你是不是 AI？' etc.

    Whitespace is stripped before matching so "你是不是 AI" still hits "是ai".
    The flag no longer short-circuits the reply — it switches on an in-character
    defence hint in the dynamic prompt, so she pushes back like a real person.
    """
    compact = _WS.sub("", text).lower()
    if any(k in compact for k in (
        "你是谁", "你是人", "你是真人", "你是活的", "是真人吗", "是人类吗",
        "是ai", "人工智能", "机器人", "是程序", "你是程序",
        "虚拟角色", "是虚拟的", "你是假的",
    )):
        return True
    low = text.lower()
    return any(k in low for k in (
        "are you human", "are you real", "are you alive", "are you a person",
        "are you an ai", "are you ai", "are you a bot", "are you a robot",
        "are you a program", "you are an ai", "you're an ai",
    ))


def classify_message(text: str) -> dict:
    """Run all safety checks. Returns a decision object."""
    return {
        "is_crisis": detect_crisis(text),
        "is_dependency": detect_dependency(text),
        "asks_identity": detect_identity_question(text),
    }
