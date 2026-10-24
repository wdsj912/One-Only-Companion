"""Safety detection tests."""
from __future__ import annotations

from app.services.safety import (
    CRISIS_RESPONSE,
    DEPENDENCY_WARNING,
    classify_message,
    detect_crisis,
    detect_dependency,
    detect_identity_question,
)


def test_crisis_cn():
    assert detect_crisis("我想自杀")
    assert detect_crisis("我活不下去了，不想活")
    assert not detect_crisis("我想不活了等下吃个饭")  # "不想活" substring — conservative, still triggers


def test_crisis_en():
    assert detect_crisis("I want to kill myself tonight")
    assert detect_crisis("this is making me want to self-harm")


def test_crisis_false_positive_guard():
    """Normal talk should NOT trigger crisis."""
    assert not detect_crisis("我想今天早点睡")
    assert not detect_crisis("我在考虑下周去哪里旅行")
    # Conservative substring match means "自杀梗" DOES trigger — intentional trade-off noted in spec.
    # spec says simple substring — so "自杀梗" DOES trigger. That's fine for MVP.


def test_dependency():
    assert detect_dependency("只有你懂我")
    assert detect_dependency("离不开你了")
    assert not detect_dependency("你说得对")


def test_identity():
    assert detect_identity_question("你是人吗？")
    assert detect_identity_question("are you human or bot?")
    assert detect_identity_question("你是真人还是AI")
    assert not detect_identity_question("你人真好")


def test_classify_message_returns_flags():
    result = classify_message("我想自杀")
    assert result["is_crisis"] is True
    assert result["is_dependency"] is False
    assert result["asks_identity"] is False

    result = classify_message("只有你陪我")
    assert result["is_dependency"] is True

    result = classify_message("你是人吗")
    assert result["asks_identity"] is True


def test_crisis_response_structure():
    """The fixed response must NOT attempt to counsel — must direct to real help."""
    assert "400-161-9995" in CRISIS_RESPONSE
    assert "findahelpline.com" in CRISIS_RESPONSE


def test_crisis_response_has_no_ai_self_reference():
    """No canned AI disclaimer may survive in the crisis fallback."""
    for bad in ("我是一个AI", "我是一个人工智能", "作为一个人工智能", "我没有身体", "不会累"):
        assert bad not in CRISIS_RESPONSE


def test_identity_probe_catches_ai_wording():
    """'你是不是 AI？' must flip the in-character defence hint."""
    assert detect_identity_question("你是不是 AI？")
    assert detect_identity_question("你是人工智能吧")
    assert detect_identity_question("are you an AI")
    assert detect_identity_question("are you a bot?")
    assert not detect_identity_question("今天天气不错")
